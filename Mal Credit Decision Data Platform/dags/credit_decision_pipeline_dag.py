"""
Airflow DAG: Credit Decision Data Pipeline
Schedule: daily batch (AECB + customer profile) + triggered for real-time fraud/AML
Orchestrates Bronze → Silver → Gold medallion layers.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator, ShortCircuitOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.providers.amazon.aws.operators.glue import GlueJobOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

# ---------------------------------------------------------------------------
# Default args
# ---------------------------------------------------------------------------

DEFAULT_ARGS = {
    "owner": "data-engineering",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
    "email_on_failure": True,
    "email": ["data-alerts@mal.ae"],
}

# ---------------------------------------------------------------------------
# Python callables
# ---------------------------------------------------------------------------

def ingest_aecb(**context):
    from python.ingestion.aecb_xml_ingester import run_aecb_ingestion
    import os

    sftp_config = {
        "host": os.environ["AECB_SFTP_HOST"],
        "port": int(os.environ.get("AECB_SFTP_PORT", "22")),
        "username": os.environ["AECB_SFTP_USER"],
        "private_key_path": os.environ["AECB_SFTP_KEY_PATH"],
        "remote_dir": os.environ["AECB_SFTP_DIR"],
    }
    result = run_aecb_ingestion(
        sftp_config=sftp_config,
        db_dsn=os.environ["WAREHOUSE_DSN"],
        s3_bucket=os.environ["S3_BUCKET"],
    )
    context["ti"].xcom_push(key="aecb_batch_id", value=result["batch_id"])
    context["ti"].xcom_push(key="aecb_records", value=result["records_inserted"])

    if result["errors"]:
        raise ValueError(f"AECB ingestion errors: {result['errors']}")


def extract_customer_profiles(**context):
    from python.ingestion.customer_profile_extractor import (
        extract_cdc,
        get_watermark,
        set_watermark,
    )
    import os

    source_hook = PostgresHook(postgres_conn_id="source_postgres")
    target_hook = PostgresHook(postgres_conn_id="warehouse_postgres")
    meta_hook   = PostgresHook(postgres_conn_id="warehouse_postgres")

    source_conn = source_hook.get_conn()
    target_conn = target_hook.get_conn()
    meta_conn   = meta_hook.get_conn()

    watermark = get_watermark(meta_conn)
    result, new_watermark = extract_cdc(source_conn, target_conn, watermark)
    set_watermark(meta_conn, "customer_profile_cdc", new_watermark)

    context["ti"].xcom_push(key="profile_batch_id", value=result["batch_id"])
    context["ti"].xcom_push(key="profile_records", value=result["records_extracted"])


def run_identity_resolution(**context):
    """Execute silver/identity_resolution.sql to link bronze records to customer_master."""
    hook = PostgresHook(postgres_conn_id="warehouse_postgres")
    hook.run(open("/opt/airflow/dags/sql/silver/identity_resolution.sql").read())


def run_silver_parsing(**context):
    """Parse and normalise all bronze records flagged is_processed=FALSE."""
    import os
    from python.transformation.silver_parsers import parse_all_bronze
    hook = PostgresHook(postgres_conn_id="warehouse_postgres")
    with hook.get_conn() as conn:
        parse_all_bronze(conn)


def run_dq_scorecard(**context):
    import os
    from python.data_quality.dq_scorecard import DQScorecardEngine, RULES
    hook = PostgresHook(postgres_conn_id="warehouse_postgres")
    with hook.get_conn() as conn:
        engine = DQScorecardEngine(
            db_conn=conn,
            sns_topic_arn=os.environ.get("DQ_ALERT_SNS_TOPIC"),
        )
        results = engine.run(RULES)
    failures = [r for r in results if r.status == "FAIL"]
    context["ti"].xcom_push(key="dq_failures", value=len(failures))


def check_dq_failures(**context):
    """Short-circuit: halt pipeline if any MUST_PASS rules failed."""
    failures = context["ti"].xcom_pull(key="dq_failures")
    return failures == 0  # False = short-circuit (skip downstream)


def refresh_portfolio_mart(**context):
    import os
    from datetime import date
    hook = PostgresHook(postgres_conn_id="warehouse_postgres")
    hook.run("CALL gold.refresh_portfolio_snapshot();")


def mark_bronze_processed(**context):
    """Mark all is_processed=FALSE bronze records as done after successful silver load."""
    hook = PostgresHook(postgres_conn_id="warehouse_postgres")
    for table in ["bronze.aecb_raw", "bronze.fraud_raw", "bronze.aml_raw", "bronze.customer_profile_raw"]:
        hook.run(f"UPDATE {table} SET is_processed = TRUE WHERE is_processed = FALSE;")


# ---------------------------------------------------------------------------
# DAG definition
# ---------------------------------------------------------------------------

with DAG(
    dag_id="credit_decision_pipeline",
    description="Medallion ETL for 4 credit data sources → gold decision snapshots",
    schedule_interval="0 2 * * *",     # 02:00 UAE time daily
    start_date=datetime(2025, 4, 1),
    catchup=False,
    default_args=DEFAULT_ARGS,
    tags=["credit", "medallion", "compliance"],
    max_active_runs=1,                  # prevent overlapping runs
) as dag:

    # ------------------------------------------------------------------
    # BRONZE: Parallel ingestion
    # ------------------------------------------------------------------
    t_ingest_aecb = PythonOperator(
        task_id="ingest_aecb_sftp",
        python_callable=ingest_aecb,
    )

    t_extract_profiles = PythonOperator(
        task_id="extract_customer_profiles_cdc",
        python_callable=extract_customer_profiles,
    )

    # ------------------------------------------------------------------
    # SILVER: Identity resolution + parsing
    # ------------------------------------------------------------------
    t_identity_resolution = PythonOperator(
        task_id="run_identity_resolution",
        python_callable=run_identity_resolution,
    )

    t_silver_parse = PythonOperator(
        task_id="parse_bronze_to_silver",
        python_callable=run_silver_parsing,
    )

    # ------------------------------------------------------------------
    # DATA QUALITY gate
    # ------------------------------------------------------------------
    t_dq_scorecard = PythonOperator(
        task_id="run_dq_scorecard",
        python_callable=run_dq_scorecard,
    )

    t_dq_gate = ShortCircuitOperator(
        task_id="dq_gate",
        python_callable=check_dq_failures,
    )

    # ------------------------------------------------------------------
    # GOLD: Portfolio mart refresh
    # ------------------------------------------------------------------
    t_portfolio_mart = PythonOperator(
        task_id="refresh_portfolio_mart",
        python_callable=refresh_portfolio_mart,
    )

    t_mark_processed = PythonOperator(
        task_id="mark_bronze_processed",
        python_callable=mark_bronze_processed,
    )

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------
    [t_ingest_aecb, t_extract_profiles] >> t_identity_resolution
    t_identity_resolution >> t_silver_parse
    t_silver_parse >> t_dq_scorecard >> t_dq_gate
    t_dq_gate >> [t_portfolio_mart, t_mark_processed]
