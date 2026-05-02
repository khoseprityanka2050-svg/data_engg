# Mal Credit Decision Data Platform

Pipeline for credit decisions across three products:
**Personal Finance · BNPL · Credit Card Alternative**

Built on a medallion architecture (Bronze → Silver → Gold) on AWS.
Targets 10K decisions/day at launch, with a clear path to 100K/day by month 12.

---

## Why this structure?

The core problem is simple: we have four data sources that each identify the same customer differently. AECB knows them by Emirates ID. The fraud vendor knows them by phone + email. AML knows them by name + date of birth. Our own database knows them by an internal UUID. Before any credit decision can be made, we need to stitch these four views into one.

That's the piece I spent the most time on — the identity resolution layer. Everything else flows from getting that right.

The medallion approach (bronze → silver → gold) was a deliberate choice over a simpler "load and transform" design. The main reason: CBUAE requires a complete, immutable audit trail. If we transform in place, we lose the original data. With medallion, bronze is a permanent record of exactly what arrived from each source, and gold is the decision-ready view. We can always trace a credit decision back to the raw bytes on S3.

---

## Repository Layout

```
.
├── sql/
│   ├── bronze/            # DDL for raw landing tables (one per source)
│   ├── silver/            # Parsed + entity-resolved tables
│   ├── gold/              # Decision snapshots + portfolio mart
│   └── data_quality/      # DQ rules and scorecard table
│
├── python/
│   ├── ingestion/
│   │   ├── aecb_xml_ingester.py          # SFTP batch → XML → bronze
│   │   ├── fraud_api_ingester.py         # REST API call → JSON → bronze
│   │   ├── aml_webhook_handler.py        # Incoming webhook → JSON → bronze
│   │   └── customer_profile_extractor.py # PostgreSQL CDC → bronze
│   ├── transformation/
│   │   └── identity_resolver.py          # The cross-source matching logic
│   └── data_quality/
│       └── dq_scorecard.py               # Rule evaluator + alerting
│
└── dags/
    └── credit_decision_pipeline_dag.py   # Airflow orchestration
```

---

## Data Sources

### 1. AECB (UAE Credit Bureau)
- Daily SFTP batch, XML format
- Matched on Emirates ID — the most reliable key we have
- Key signals: credit score (300–900), delinquency history, credit utilisation, inquiry count in last 6 months
- One tricky part: the XML uses namespaces (`http://www.aecb.gov.ae/schema/v2`) which broke the first XML parser I tried — had to switch to ElementTree with explicit namespace handling

### 2. Fraud Detection Provider
- Real-time REST API, called per application
- Matched on phone + email
- Score comes back as 0–1 float; I added a banding layer on top (LOW/MEDIUM/HIGH/CRITICAL) because the raw float is hard to action in a policy rule

### 3. AML / PEP Screening
- Webhook callback model — we request a screen, provider sends results async
- Matched on name + date of birth which is the hardest matching problem in this pipeline
- Idempotency is critical here: providers retry failed webhooks, so we need to safely ignore duplicates

### 4. Internal Customer Profile
- PostgreSQL source database, CDC via `updated_at` watermark
- The authoritative source for income, employment, and Emirates ID
- Full load on first run, incremental from thereon

---

## Identity Resolution

This is the most interesting design decision in the whole system. Since each source uses a different customer identifier, we need a priority-ordered matching strategy:

```
Tier 1: internal_uuid   → exact match, confidence 1.0
         ↓ (no match)
Tier 2: emirates_id     → exact match, confidence 0.99
         ↓ (no match)
Tier 3: phone + email   → both must match, confidence 0.90
         ↓ (no match)
Tier 4: name + DOB      → fuzzy name similarity ≥ 0.85, exact DOB, confidence 0.85
         ↓ (no match)
         create new customer record
```

I landed on 0.85 as the name similarity threshold after testing on about 200 sample records. The tricky cases were Arabic names that get transliterated differently across documents (e.g. "Mohammed" vs "Mohammad" vs "Mohamed"). Below 0.85 we got too many false positives. Above 0.90 we started missing real matches.

Any conflict between what a source says and what we already have stored (e.g. same UUID but different Emirates IDs) gets logged to `silver.identity_resolution_exceptions` and triggers an alert. We don't silently overwrite.

---

## Data Quality

I defined two levels of DQ rule:

- **MUST_PASS**: pipeline halts if these fail. No point creating a decision record with broken inputs.
- **HIGH**: SNS alert fires, pipeline continues, decision gets a low `dq_score`.

| Rule | Source | Level | Threshold |
|---|---|---|---|
| DQ-AECB-01 | aecb_parsed | MUST_PASS | 100% — Emirates ID format |
| DQ-AECB-02 | aecb_parsed | MUST_PASS | 100% — Credit score range |
| DQ-AECB-03 | aecb_parsed | HIGH | 95% — Report not older than 90 days |
| DQ-FRAUD-01 | fraud_scores | MUST_PASS | 100% — Score in [0,1] |
| DQ-FRAUD-02 | fraud_scores | HIGH | 99% — Scored in last 24h |
| DQ-AML-01 | aml_results | MUST_PASS | 100% — Risk level present |
| DQ-CUST-01 | customer_profiles | HIGH | 98% — Income > 0 |
| DQ-GOLD-01 | credit_decision_inputs | MUST_PASS | 100% — All 4 sources linked on approvals |

The thresholds aren't arbitrary — DQ-AECB-03 at 95% allows for the occasional stale report while AECB fixes their feed, without blocking the whole pipeline. Will revisit these after the first 30 days of real data.

---

## Infrastructure (AWS)

```
AECB SFTP    ──►  S3 Bronze (KMS encrypted)  ──►  RDS PostgreSQL
Fraud API    ──►  Python / Lambda             ─┘   ├── bronze schema
AML Webhook  ──►  API Gateway + FastAPI       ─┘   ├── silver schema
PostgreSQL   ──►  CDC / watermark             ─┘   └── gold schema
                                                        │
                                                        ▼ (Day 60+)
                                                   S3 Parquet → Athena
Orchestration: MWAA (managed Airflow)
Alerting:      CloudWatch → SNS → Slack
Secrets:       AWS Secrets Manager (no credentials in code)
```

I chose MWAA over self-managed Airflow mainly because we don't want to spend engineering time managing the scheduler — at this stage the pipeline complexity doesn't justify it.

---

## Running Locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env

# Apply schemas in order (dependencies matter)
psql $DB_DSN < sql/bronze/create_bronze_tables.sql
psql $DB_DSN < sql/silver/create_silver_tables.sql
psql $DB_DSN < sql/gold/credit_decision_snapshot.sql
psql $DB_DSN < sql/gold/portfolio_monitoring_mart.sql
psql $DB_DSN < sql/data_quality/dq_rules.sql

# Test AECB ingestion with a sample file
python -c "
from python.ingestion.aecb_xml_ingester import run_aecb_ingestion
run_aecb_ingestion(sftp_config={...}, db_dsn='...', s3_bucket='...')
"

# Start AML webhook listener
uvicorn python.ingestion.aml_webhook_handler:app --port 8080
```

---

## Compliance

The CBUAE requirement that shaped everything else: every credit decision must be fully reconstructable. You need to be able to show exactly what data fed a decision, as it was at the time.

- Bronze tables are append-only — no UPDATEs, no DELETEs
- `gold.credit_decision_inputs` stores a frozen snapshot of all inputs at decision time
- Each gold row has foreign keys back to specific silver rows, which have foreign keys back to specific bronze rows
- Bronze files archived on S3 for 7 years (CBUAE credit record retention)
- All resources in `me-south-1` (Bahrain) for UAE data residency

Column-level encryption for Emirates ID and DOB is on the Day 60 plan — the schema is designed to accommodate it without changes.
