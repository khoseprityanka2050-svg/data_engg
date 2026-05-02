# Mal Credit Decision Data Platform

Unified data pipeline for credit decisions across three products:  
**Personal Finance · BNPL · Credit Card Alternative**

Built on a medallion architecture (Bronze → Silver → Gold) on AWS.  
Supports **10K decisions/day at launch**, designed to scale to **100K/day within 12 months**.

---

## Repository Structure

```
.
├── sql/
│   ├── bronze/               # Raw ingestion table DDL (4 sources)
│   ├── silver/               # Parsed, normalised, entity-resolved tables
│   ├── gold/                 # Decision snapshots + portfolio mart
│   └── data_quality/         # DQ rule SQL + scorecard table
│
├── python/
│   ├── ingestion/            # One module per data source
│   │   ├── aecb_xml_ingester.py        # SFTP → XML → bronze
│   │   ├── fraud_api_ingester.py       # REST API → JSON → bronze
│   │   ├── aml_webhook_handler.py      # Webhook → JSON → bronze
│   │   └── customer_profile_extractor.py  # PostgreSQL CDC → bronze
│   ├── transformation/
│   │   └── identity_resolver.py        # 4-tier cross-source entity resolution
│   └── data_quality/
│       └── dq_scorecard.py             # Rule engine + SNS alerting
│
└── dags/
    └── credit_decision_pipeline_dag.py  # Airflow orchestration
```

---

## Architecture: Medallion Layers

### Bronze — Raw Landing Zone (S3 + RDS)

| Table | Source | Format | Match Key |
|---|---|---|---|
| `bronze.aecb_raw` | UAE Credit Bureau (SFTP) | XML | Emirates ID |
| `bronze.fraud_raw` | Fraud provider (REST API) | JSON | Phone + Email |
| `bronze.aml_raw` | AML/PEP screening (webhook) | JSON | Name + DOB |
| `bronze.customer_profile_raw` | Internal PostgreSQL (CDC) | JSONB | Internal UUID |

- Append-only; never mutated after insert  
- SHA-256 checksum on every file (tamper evidence)  
- S3 archive with KMS encryption for CBUAE audit retention

### Silver — Cleansed & Resolved

- **`silver.customer_master`** — unified customer entity across all 4 sources  
- **Identity Resolution** (4-tier priority): `internal_uuid` → `emirates_id` → `phone+email` → `name+DOB (fuzzy)`  
- Fuzzy name matching uses `pg_trgm` similarity ≥ 0.85; unmatched records routed to `silver.identity_resolution_exceptions`

### Gold — Decision-Ready

- **`gold.credit_decision_inputs`** — immutable point-in-time snapshot per credit decision  
  - Append-only; captures all 4 input signals at decision time  
  - Required for CBUAE audit: full provenance chain to bronze via foreign keys  
- **`gold.portfolio_daily_snapshot`** — daily aggregates for risk dashboards  
- **`gold.dq_scorecard`** — DQ rule results per run

---

## Data Sources

### 1. AECB (UAE Credit Bureau)
- Delivery: daily SFTP batch, XML format
- Ingestion: `python/ingestion/aecb_xml_ingester.py`
- Key signals: credit score (300–900), delinquency buckets, utilisation rate, inquiry count

### 2. Fraud Detection Provider
- Delivery: real-time REST API call per application
- Ingestion: `python/ingestion/fraud_api_ingester.py`
- Key signals: fraud score (0–1), fraud band, velocity flags, identity verified

### 3. AML / PEP Screening
- Delivery: webhook callbacks after each screening request
- Ingestion: `python/ingestion/aml_webhook_handler.py` (FastAPI endpoint)
- Key signals: PEP flag, sanctions flag, adverse media flag, overall risk level

### 4. Internal Customer Profile
- Delivery: PostgreSQL CDC (updated_at watermark)
- Ingestion: `python/ingestion/customer_profile_extractor.py`
- Key signals: income, employment, Emirates ID, nationality

---

## Identity Resolution Logic

```
1. internal_uuid  →  confidence 1.00  (authoritative internal system)
2. emirates_id    →  confidence 0.99  (UAE government-issued, exact match)
3. phone + email  →  confidence 0.90  (dual-key, both must match)
4. name + DOB     →  confidence 0.85  (fuzzy, flags for manual review)
```

Conflicts detected during resolution are logged to `silver.identity_resolution_exceptions` and trigger an SNS alert.

---

## Data Quality Rules

| Rule ID | Source | Severity | Threshold | Description |
|---|---|---|---|---|
| DQ-AECB-01 | aecb_parsed | MUST_PASS | 100% | Emirates ID format valid |
| DQ-AECB-02 | aecb_parsed | MUST_PASS | 100% | Credit score in 300–900 |
| DQ-AECB-03 | aecb_parsed | HIGH | 95% | Report date within 90 days |
| DQ-FRAUD-01 | fraud_scores | MUST_PASS | 100% | Fraud score in [0,1] |
| DQ-FRAUD-02 | fraud_scores | HIGH | 99% | Scored within last 24h |
| DQ-AML-01 | aml_results | MUST_PASS | 100% | Risk level not null |
| DQ-CUST-01 | customer_profiles | HIGH | 98% | Monthly income > 0 |
| DQ-GOLD-01 | credit_decision_inputs | MUST_PASS | 100% | All 4 source IDs present (approved) |

MUST_PASS failures halt the pipeline and send an SNS alert.

---

## Infrastructure (AWS)

```
SFTP (AECB)  ──►  S3 Bronze Bucket  ──►  Glue/Python ETL  ──►  RDS PostgreSQL
REST API     ──►  Lambda (sync)     ─┘
Webhook      ──►  API Gateway + Lambda ─┘                   ──►  S3 Gold (Parquet)
PostgreSQL   ──►  DMS / CDC         ─┘                           (for Redshift/Athena)

Orchestration: MWAA (managed Airflow)
Monitoring:    CloudWatch + SNS alerts
Secrets:       AWS Secrets Manager
```

---

## Running Locally

```bash
# Install dependencies
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Set environment variables
cp .env.example .env  # fill in DB_DSN, SFTP credentials, API keys

# Apply DB schemas (order matters)
psql $DB_DSN < sql/bronze/create_bronze_tables.sql
psql $DB_DSN < sql/silver/create_silver_tables.sql
psql $DB_DSN < sql/gold/credit_decision_snapshot.sql
psql $DB_DSN < sql/gold/portfolio_monitoring_mart.sql
psql $DB_DSN < sql/data_quality/dq_rules.sql

# Run AECB ingestion manually
python -c "
from python.ingestion.aecb_xml_ingester import run_aecb_ingestion
run_aecb_ingestion(sftp_config={...}, db_dsn='...', s3_bucket='...')
"

# Start AML webhook receiver
uvicorn python.ingestion.aml_webhook_handler:app --port 8080
```

---

## Compliance Notes

- All bronze tables are append-only (no `UPDATE`/`DELETE`); suitable for CBUAE immutability requirements  
- `gold.credit_decision_inputs` stores a complete point-in-time snapshot of every input used in a credit decision  
- S3 files retained with KMS encryption and versioning enabled  
- Data residency: all resources in `me-south-1` (Bahrain) — nearest to UAE  
- Access control: column-level encryption for PII fields planned in Phase 2 (Day 60–90)
