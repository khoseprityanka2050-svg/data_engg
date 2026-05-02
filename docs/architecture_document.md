# Credit Decision Data Architecture
## Mal — Personal Finance · BNPL · Credit Card Alternative
### Data Engineering Assessment — Architecture Document

---

## PAGE 1: Executive Summary & System Overview

### Business Context

Mal is launching three credit products in Q2 2025. Each credit decision must combine signals from four external/internal data sources that arrive in conflicting formats, on different schedules, and with different customer identifiers. The platform must be audit-ready from day one for UAE Central Bank (CBUAE) compliance.

### Core Design Principles

| Principle | Implementation |
|---|---|
| Immutability | Bronze tables append-only; gold decision snapshots never updated |
| Traceability | Every approved decision traces back to a specific bronze row in each source |
| Resilience | Each source fails independently; missing data degrades gracefully |
| Compliance | Full audit trail, data residency in AWS me-south-1 (Bahrain) |
| Scalability | 10K decisions/day at launch → 100K/day within 12 months, no re-architecture |

### High-Level Data Flow

```
┌─────────────┐  XML/SFTP   ┌─────────────────────────────────────────────────┐
│ AECB Bureau │ ──────────► │                  BRONZE LAYER                   │
└─────────────┘             │  (Raw landing zone — S3 + RDS, append-only)     │
┌─────────────┐  REST/JSON  │  aecb_raw │ fraud_raw │ aml_raw │ profile_raw   │
│ Fraud API   │ ──────────► │                                                  │
└─────────────┘             └─────────────────┬───────────────────────────────┘
┌─────────────┐  Webhook    ┌─────────────────▼───────────────────────────────┐
│ AML/PEP     │ ──────────► │                  SILVER LAYER                   │
└─────────────┘             │  (Parsed, normalised, entity-resolved)           │
┌─────────────┐  CDC/PG     │  customer_master · aecb_parsed · fraud_scores   │
│ Internal DB │ ──────────► │  aml_results · customer_profiles                │
└─────────────┘             └─────────────────┬───────────────────────────────┘
                            ┌─────────────────▼───────────────────────────────┐
                            │                   GOLD LAYER                    │
                            │  credit_decision_inputs (immutable snapshots)   │
                            │  portfolio_daily_snapshot · dq_scorecard        │
                            └─────────────────────────────────────────────────┘
```

---

## PAGE 2: Data Sources — Ingestion Strategy

### Source Comparison Matrix

| Attribute | AECB | Fraud Provider | AML/PEP | Internal Profile |
|---|---|---|---|---|
| Protocol | SFTP | REST API | Webhook | PostgreSQL |
| Format | XML | JSON | JSON | Relational rows |
| Cadence | Daily batch | Real-time per decision | Async callback | CDC / watermark |
| Match key | Emirates ID | Phone + Email | Name + DOB | Internal UUID |
| Latency SLA | T+1 day | < 2 seconds | < 5 minutes | Near-real-time |
| Failure mode | File missing | HTTP timeout | Missed webhook | Replication lag |
| Volume (launch) | ~3K records/day | ~10K calls/day | ~10K events/day | ~2K changes/day |

### Ingestion Patterns

**AECB (Batch SFTP)**
- Paramiko SFTP client polls at 01:00 UAE time
- Each XML file archived to `s3://mal-bronze/aecb/YYYY/MM/DD/` with KMS-SSE
- SHA-256 checksum stored with every row; file-level idempotency via `processed_` marker
- Parsed into one bronze row per `<CreditReport>` element (one customer)

**Fraud API (Synchronous Real-time)**
- Called inline at application submission; 5-second timeout, 3 retries with linear backoff
- Response persisted to `bronze.fraud_raw` before decisioning logic reads it
- Ensures fraud input is immutable even if provider data changes after the fact

**AML/PEP (Webhook)**
- FastAPI endpoint behind AWS API Gateway
- Idempotency enforced on `webhook_event_id` (ON CONFLICT DO NOTHING)
- Unprocessed webhooks retried by provider; endpoint returns 200 for duplicates

**Customer Profile (PostgreSQL CDC)**
- `updated_at` watermark-based extraction (no dependency on Debezium/DMS at launch)
- Full load on first run; incremental CDC on subsequent runs
- Watermark stored in `pipeline_watermarks` metadata table

---

## PAGE 3: Bronze Layer — Design & Schema

### Design Decisions

**Append-only** — No UPDATE or DELETE ever executed on bronze tables. Satisfies CBUAE requirement for immutable audit records.

**Schema-on-read** — Raw XML/JSON stored in `TEXT`/`JSONB` columns alongside extracted match keys. Allows reprocessing if parse logic changes without data loss.

**Batch grouping** — Every row tagged with a `batch_id` UUID. Allows complete batch replay if a silver parse job fails partway through.

**Checksum** — SHA-256 of the raw file stored on every AECB row. Detects tampering in transit or at rest.

### Bronze Table Summary

```sql
-- All 4 tables share this pattern:
--   raw payload (TEXT or JSONB)     -- never touched after insert
--   match key (source-specific)     -- for linking to silver
--   batch_id + ingested_at          -- for replay and audit
--   is_processed + processing_error -- pipeline state
--   checksum (AECB only)            -- tamper evidence
```

| Table | Match Key | Payload Column | Idempotency |
|---|---|---|---|
| `bronze.aecb_raw` | `emirates_id` | `raw_xml TEXT` | SHA-256 dedup |
| `bronze.fraud_raw` | `phone + email` | `raw_response JSONB` | `api_request_id` |
| `bronze.aml_raw` | `name + DOB` | `raw_payload JSONB` | `webhook_event_id UNIQUE` |
| `bronze.customer_profile_raw` | `internal_uuid` | `raw_record JSONB` | CDC watermark |

### Retention Policy

| Storage | Retention | Reason |
|---|---|---|
| S3 (raw files) | 7 years | CBUAE credit record retention requirement |
| `bronze.*` tables | 2 years hot, archived to S3 Glacier after | Cost optimisation; queryable via Athena |

---

## PAGE 4: Silver Layer — Parsing & Identity Resolution

### Identity Resolution: 4-Tier Cascade

The hardest problem in this architecture is that each source identifies customers differently. The resolver tries match keys in priority order, creates a new `customer_key` only if all tiers fail.

```
Tier 1: internal_uuid          confidence=1.00  ← authoritative
         │ (exact match on silver.customer_master)
         ▼ no match
Tier 2: emirates_id            confidence=0.99  ← UAE government-issued
         │ (exact match)
         ▼ no match
Tier 3: phone_e164 + email     confidence=0.90  ← dual-key required
         │ (both must match; phone normalised to E.164)
         ▼ no match
Tier 4: full_name + DOB        confidence=0.85  ← fuzzy
         │ (pg_trgm similarity ≥ 0.85, exact DOB)
         │ → flags result for manual review
         ▼ no match
         CREATE new customer_key
```

**Conflict detection** — When a higher-confidence match is found but incoming data contradicts stored fields (e.g. different Emirates ID on same UUID), the conflict is logged to `silver.identity_resolution_exceptions` and surfaced as a DQ alert.

### Silver Table Design

- **`silver.customer_master`** — one row per unique customer; updated as new source data arrives. Stores the canonical identity fields and which resolution method created the record.
- **`silver.aecb_parsed`** — structured credit bureau signals extracted from XML; foreign-keyed to `customer_master`
- **`silver.fraud_scores`** — normalised fraud signals; `fraud_band` derived from score thresholds (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`)
- **`silver.aml_results`** — boolean PEP/sanctions flags + structured match details in JSONB
- **`silver.customer_profiles`** — versioned profile snapshots; `profile_version` increments on each CDC change

### Phone Normalisation

All phone numbers normalised to E.164 before storage:
- `050 123 4567` → `+971501234567`
- `00971501234567` → `+971501234567`
- Enables exact-match joins across fraud and profile sources

---

## PAGE 5: Gold Layer — Decision Traceability

### Immutable Decision Snapshot

`gold.credit_decision_inputs` captures the **exact state of every input** at the moment a credit decision is requested. This is the core compliance artefact.

**Why point-in-time snapshots matter:**  
If a customer's AECB score changes after a decision, the audit record must show what the score *was* at decision time — not the current value. This table is append-only and denormalised by design.

### Snapshot Contents

```
gold.credit_decision_inputs
├── Identity
│   ├── decision_id (UUID, PK)
│   ├── customer_key → silver.customer_master
│   ├── application_id
│   └── product_type (personal_finance | bnpl | credit_card_alternative)
│
├── Source Timestamps (when each datum was valid)
│   ├── aecb_report_date
│   ├── fraud_scored_at
│   ├── aml_screening_date
│   └── profile_as_of
│
├── AECB Inputs (9 fields — all copied at snapshot time)
├── Fraud Inputs (4 fields)
├── AML Inputs (4 fields)
├── Customer Profile Inputs (4 fields)
│
├── Derived Ratios (computed at snapshot time, not recalculated)
│   └── dti_ratio = (requested_amount / tenor) / monthly_income
│
├── Data Quality
│   ├── dq_score (0-1 composite)
│   └── dq_flags (JSONB — list of failed rules)
│
├── Decision Outcome (written by decisioning engine)
│   ├── decision_outcome (APPROVED | DECLINED | REFERRED | PENDING)
│   ├── decision_reason_codes TEXT[]
│   └── decided_by (model version or analyst ID)
│
└── Silver Foreign Keys (traceability chain)
    ├── silver_aecb_id → silver.aecb_parsed.id
    ├── silver_fraud_id → silver.fraud_scores.id
    ├── silver_aml_id → silver.aml_results.id
    └── silver_profile_id → silver.customer_profiles.id
```

### Full Audit Chain

```
CBUAE Auditor asks: "Why was application APP-001 approved on 2025-06-15?"

gold.credit_decision_inputs (decision_id=..., application_id=APP-001)
  → silver_aecb_id  → silver.aecb_parsed  → bronze_id → bronze.aecb_raw (raw XML)
  → silver_fraud_id → silver.fraud_scores → bronze_id → bronze.fraud_raw (raw JSON)
  → silver_aml_id   → silver.aml_results  → bronze_id → bronze.aml_raw (raw webhook)
  → silver_profile_id → silver.customer_profiles → bronze_id → bronze.customer_profile_raw
```

Every step in the chain is queryable. Raw payloads are preserved in bronze indefinitely.

---

## PAGE 6: Data Quality Framework

### DQ Rule Taxonomy

| Severity | Meaning | On Failure |
|---|---|---|
| MUST_PASS | Decision cannot be made without this | Pipeline halted, SNS alert |
| HIGH | Decision quality significantly degraded | SNS alert, continue |
| MEDIUM | Worth tracking, low immediate risk | Dashboard flag |
| LOW | Informational | Log only |

### Rule Catalogue

| Rule ID | Source | Severity | Threshold | Check |
|---|---|---|---|---|
| DQ-AECB-01 | aecb_parsed | MUST_PASS | 100% | Emirates ID is 15-18 digits |
| DQ-AECB-02 | aecb_parsed | MUST_PASS | 100% | Credit score in 300–900 |
| DQ-AECB-03 | aecb_parsed | HIGH | 95% | Report date within 90 days |
| DQ-FRAUD-01 | fraud_scores | MUST_PASS | 100% | Fraud score in [0.0, 1.0] |
| DQ-FRAUD-02 | fraud_scores | HIGH | 99% | Scored within last 24 hours |
| DQ-AML-01 | aml_results | MUST_PASS | 100% | Risk level not null |
| DQ-CUST-01 | customer_profiles | HIGH | 98% | Monthly income > 0 |
| DQ-GOLD-01 | credit_decision_inputs | MUST_PASS | 100% | All 4 source IDs present (approved) |

### DQ Scorecard Flow

```
After each silver load:
  DQScorecardEngine.run()
     ├── Evaluates all rules via SQL
     ├── Writes results to gold.dq_scorecard
     ├── MUST_PASS / HIGH failures → SNS → PagerDuty / email
     └── MUST_PASS halt_on_fail=True → raises exception → Airflow marks task FAILED
                                                         → downstream tasks skipped
```

### Composite DQ Score per Decision

Each `gold.credit_decision_inputs` row carries a `dq_score` (0–1) computed as:
- 1.0 if all 4 source IDs present and all inputs within freshness thresholds
- Deducted per missing or stale source signal
- Decisions with `dq_score < 0.7` flagged in `dq_flags` JSONB for underwriter review

---

## PAGE 7: Portfolio Monitoring Mart

### Purpose

The `gold.portfolio_daily_snapshot` table feeds the risk team's operational dashboards. It is refreshed daily via `CALL gold.refresh_portfolio_snapshot()` and exposes pre-aggregated metrics to avoid expensive ad-hoc queries against the full decision table.

### Key Metrics Tracked

**Volume & Approval Rates**
- `total_applications`, `total_approvals`, `approval_rate`
- `approval_rate_7d_avg` (rolling 7-day, computed in view)

**Credit Quality Distribution**
- % of applicants in score bands: `<600`, `600–699`, `≥700`
- Average credit score per product per day

**Risk Concentration**
- `v_risk_concentration` view: cross-tabs score band × DTI band × product
- `pct_dti_above_50`: proportion of applicants with DTI > 50% (CBUAE concern)
- `pct_fraud_high_critical`: early warning on fraud ring activity

**AML Exposure**
- `pct_pep`, `pct_sanctioned`: tracked daily; threshold breaches trigger compliance alert

**Data Quality**
- `avg_dq_score`, `pct_low_dq` (decisions where `dq_score < 0.7`)

### Dashboard Use Cases

| Dashboard | Audience | Refresh | Primary Source |
|---|---|---|---|
| Daily Volume & Approval | Operations | Real-time | `v_latest_decisions` |
| Credit Quality Trends | Risk team | Daily | `v_approval_rate_trend` |
| Risk Concentration | Chief Risk Officer | Daily | `v_risk_concentration` |
| AML Exposure | Compliance | Daily | `portfolio_daily_snapshot` |
| DQ Health | Data Engineering | Per pipeline run | `v_dq_alerts` |

---

## PAGE 8: Infrastructure & Compliance

### AWS Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │              AWS me-south-1 (Bahrain)        │
                    │                                              │
  AECB SFTP ───────►│  S3 Bronze (KMS-SSE, versioning ON)         │
  Fraud API ───────►│  ├── aecb/YYYY/MM/DD/                       │
  Webhook   ───────►│  ├── fraud/YYYY/MM/DD/                      │
  Postgres  ───────►│  └── aml/YYYY/MM/DD/                        │
                    │                │                             │
                    │                ▼                             │
                    │  MWAA (managed Airflow)                      │
                    │  └── credit_decision_pipeline DAG            │
                    │                │                             │
                    │                ▼                             │
                    │  RDS PostgreSQL (Multi-AZ)                   │
                    │  ├── schema: bronze                          │
                    │  ├── schema: silver                          │
                    │  └── schema: gold                            │
                    │                │                             │
                    │                ▼ (Day 60+)                   │
                    │  S3 Gold (Parquet) → Athena / Redshift       │
                    │                                              │
                    │  SNS → PagerDuty (DQ alerts)                 │
                    │  Secrets Manager (API keys, DB credentials)  │
                    │  CloudWatch (pipeline metrics + dashboards)  │
                    └──────────────────────────────────────────────┘
```

### Scalability Path: 10K → 100K decisions/day

| Phase | Decisions/day | Architecture |
|---|---|---|
| Launch (Day 1–30) | 10K | RDS PostgreSQL, single Airflow worker |
| Growth (Day 60) | 30K | Read replicas for gold queries, Airflow auto-scaling |
| Scale (Day 90) | 100K | S3 + Parquet gold layer, Athena for analytics; RDS → Aurora Serverless v2 |

**Key inflection point at 30K/day:** Move gold analytics queries off RDS onto Athena (query S3 Parquet). Decisioning writes stay on RDS for low-latency inserts.

### CBUAE Compliance Controls

| Requirement | Implementation |
|---|---|
| Immutable audit trail | Bronze append-only; gold decision snapshots append-only |
| Decision explainability | `decision_reason_codes TEXT[]` on every gold row |
| Data residency (UAE) | All resources in `me-south-1`; cross-region replication disabled |
| Credit record retention | S3 lifecycle: Standard → Glacier after 2 years; 7-year total |
| Access control | IAM roles per service; RDS column-level grants; no shared credentials |
| PII protection | Secrets Manager for credentials; column encryption for Emirates ID/DOB (Phase 2) |
| Lineage | `silver_*_id` FKs on gold → silver → bronze (raw payload) on every decision |

### Security

- SFTP private key in Secrets Manager; rotated quarterly
- Fraud API key in Secrets Manager; never in environment variables or code
- Webhook secret validated on every incoming request (HTTP 401 on mismatch)
- VPC-only RDS; no public endpoint
- S3 bucket policy: `DenyUnencryptedObjectUploads`

---

*Document prepared for Mal Credit Decision Data Platform — Q2 2025 launch*
