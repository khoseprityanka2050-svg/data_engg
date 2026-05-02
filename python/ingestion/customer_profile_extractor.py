"""
Internal Customer Profile Extractor
Source: PostgreSQL operational DB, matched on internal UUID
Supports full load and CDC (change-data-capture via updated_at watermark).
Lands records in bronze.customer_profile_raw as JSONB snapshots.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator

import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values, Json

logger = logging.getLogger(__name__)

_BATCH_SIZE = 500


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def _row_to_json(row: dict) -> str:
    return json.dumps(row, cls=DecimalEncoder, default=str)


def extract_full_load(source_conn, target_conn) -> dict:
    """Full snapshot extraction — used for initial load."""
    batch_id = str(uuid.uuid4())
    total = 0
    extracted_at = datetime.now(tz=timezone.utc)

    with source_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as src_cur:
        src_cur.execute(
            """
            SELECT
                internal_uuid,
                full_name,
                date_of_birth,
                nationality,
                emirates_id,
                phone_e164,
                email,
                monthly_income_aed,
                employment_status,
                employer_name,
                years_employed,
                residence_city,
                updated_at
            FROM customers
            ORDER BY internal_uuid
            """
        )
        while True:
            rows = src_cur.fetchmany(_BATCH_SIZE)
            if not rows:
                break

            bronze_rows = [
                (
                    row["internal_uuid"],
                    Json(dict(row)),
                    row["updated_at"],
                    extracted_at,
                    batch_id,
                    "full_load",
                )
                for row in rows
            ]

            with target_conn.cursor() as tgt_cur:
                execute_values(
                    tgt_cur,
                    """
                    INSERT INTO bronze.customer_profile_raw
                        (internal_uuid, raw_record, source_updated_at, extracted_at, batch_id, extraction_method)
                    VALUES %s
                    ON CONFLICT DO NOTHING
                    """,
                    bronze_rows,
                )
            target_conn.commit()
            total += len(rows)
            logger.info("Full load: %d rows inserted (batch %s)", total, batch_id)

    return {"batch_id": batch_id, "records_extracted": total, "method": "full_load"}


def extract_cdc(source_conn, target_conn, watermark: datetime) -> tuple[dict, datetime]:
    """
    Incremental extraction via updated_at watermark.
    Returns the new high-watermark for the caller to persist.
    """
    batch_id = str(uuid.uuid4())
    total = 0
    new_watermark = watermark
    extracted_at = datetime.now(tz=timezone.utc)

    with source_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as src_cur:
        src_cur.execute(
            """
            SELECT
                internal_uuid, full_name, date_of_birth, nationality,
                emirates_id, phone_e164, email, monthly_income_aed,
                employment_status, employer_name, years_employed,
                residence_city, updated_at
            FROM customers
            WHERE updated_at > %s
            ORDER BY updated_at ASC
            """,
            (watermark,),
        )
        while True:
            rows = src_cur.fetchmany(_BATCH_SIZE)
            if not rows:
                break

            bronze_rows = [
                (
                    row["internal_uuid"],
                    Json(dict(row)),
                    row["updated_at"],
                    extracted_at,
                    batch_id,
                    "cdc",
                )
                for row in rows
            ]

            with target_conn.cursor() as tgt_cur:
                execute_values(
                    tgt_cur,
                    """
                    INSERT INTO bronze.customer_profile_raw
                        (internal_uuid, raw_record, source_updated_at, extracted_at, batch_id, extraction_method)
                    VALUES %s
                    """,
                    bronze_rows,
                )
            target_conn.commit()

            new_watermark = max(new_watermark, rows[-1]["updated_at"])
            total += len(rows)
            logger.info("CDC: %d rows (watermark=%s, batch=%s)", total, new_watermark, batch_id)

    return {"batch_id": batch_id, "records_extracted": total, "method": "cdc"}, new_watermark


def get_watermark(meta_conn, pipeline_name: str = "customer_profile_cdc") -> datetime:
    with meta_conn.cursor() as cur:
        cur.execute(
            "SELECT last_watermark FROM pipeline_watermarks WHERE pipeline_name = %s",
            (pipeline_name,),
        )
        row = cur.fetchone()
        if row:
            return row[0]
    return datetime(2000, 1, 1, tzinfo=timezone.utc)


def set_watermark(meta_conn, pipeline_name: str, watermark: datetime):
    with meta_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_watermarks (pipeline_name, last_watermark)
            VALUES (%s, %s)
            ON CONFLICT (pipeline_name) DO UPDATE SET last_watermark = EXCLUDED.last_watermark
            """,
            (pipeline_name, watermark),
        )
    meta_conn.commit()
