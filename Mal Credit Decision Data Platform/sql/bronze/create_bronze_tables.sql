-- =============================================================================
-- BRONZE LAYER: Raw ingestion tables (append-only, schema-on-read friendly)
-- All sources land here with minimal transformation. Full audit trail.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. AECB (UAE Credit Bureau) — batch SFTP, XML delivery
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bronze.aecb_raw (
    id                  BIGSERIAL PRIMARY KEY,
    emirates_id         VARCHAR(18)  NOT NULL,           -- UAE national ID
    raw_xml             TEXT         NOT NULL,           -- full XML payload
    file_name           VARCHAR(512) NOT NULL,           -- source SFTP filename
    file_received_at    TIMESTAMPTZ  NOT NULL,           -- when file landed on S3
    ingested_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    batch_id            UUID         NOT NULL,           -- groups files in one SFTP drop
    checksum_sha256     CHAR(64)     NOT NULL,           -- tamper-evidence
    is_processed        BOOLEAN      NOT NULL DEFAULT FALSE,
    processing_error    TEXT
);

CREATE INDEX idx_aecb_raw_emirates_id   ON bronze.aecb_raw (emirates_id);
CREATE INDEX idx_aecb_raw_batch_id      ON bronze.aecb_raw (batch_id);
CREATE INDEX idx_aecb_raw_ingested_at   ON bronze.aecb_raw (ingested_at);

-- ---------------------------------------------------------------------------
-- 2. Fraud Detection Provider — REST API, real-time JSON
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bronze.fraud_raw (
    id                  BIGSERIAL PRIMARY KEY,
    request_phone       VARCHAR(20)  NOT NULL,
    request_email       VARCHAR(320) NOT NULL,
    raw_response        JSONB        NOT NULL,           -- full API JSON response
    http_status_code    SMALLINT     NOT NULL,
    api_request_id      VARCHAR(128),                   -- provider's correlation ID
    requested_at        TIMESTAMPTZ  NOT NULL,
    responded_at        TIMESTAMPTZ  NOT NULL,
    latency_ms          INTEGER,
    ingested_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    batch_id            UUID         NOT NULL,
    is_processed        BOOLEAN      NOT NULL DEFAULT FALSE,
    processing_error    TEXT
);

CREATE INDEX idx_fraud_raw_phone       ON bronze.fraud_raw (request_phone);
CREATE INDEX idx_fraud_raw_email       ON bronze.fraud_raw (request_email);
CREATE INDEX idx_fraud_raw_ingested_at ON bronze.fraud_raw (ingested_at);

-- ---------------------------------------------------------------------------
-- 3. AML / PEP Screening — webhook callbacks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bronze.aml_raw (
    id                  BIGSERIAL PRIMARY KEY,
    customer_name       VARCHAR(512) NOT NULL,
    date_of_birth       DATE         NOT NULL,
    raw_payload         JSONB        NOT NULL,           -- full webhook body
    webhook_event_id    VARCHAR(256) NOT NULL UNIQUE,   -- idempotency key
    webhook_received_at TIMESTAMPTZ  NOT NULL,
    provider_timestamp  TIMESTAMPTZ,
    ingested_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    batch_id            UUID         NOT NULL,
    is_processed        BOOLEAN      NOT NULL DEFAULT FALSE,
    processing_error    TEXT
);

CREATE INDEX idx_aml_raw_name          ON bronze.aml_raw (customer_name);
CREATE INDEX idx_aml_raw_dob           ON bronze.aml_raw (date_of_birth);
CREATE INDEX idx_aml_raw_ingested_at   ON bronze.aml_raw (ingested_at);

-- ---------------------------------------------------------------------------
-- 4. Internal Customer Profile — PostgreSQL operational DB
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bronze.customer_profile_raw (
    id                  BIGSERIAL PRIMARY KEY,
    internal_uuid       UUID         NOT NULL,          -- primary key from source
    raw_record          JSONB        NOT NULL,           -- full row snapshot as JSON
    source_updated_at   TIMESTAMPTZ  NOT NULL,          -- source system's updated_at
    extracted_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    batch_id            UUID         NOT NULL,
    extraction_method   VARCHAR(32)  NOT NULL            -- 'full_load' | 'cdc'
        CHECK (extraction_method IN ('full_load', 'cdc')),
    is_processed        BOOLEAN      NOT NULL DEFAULT FALSE,
    processing_error    TEXT
);

CREATE INDEX idx_cust_raw_uuid         ON bronze.customer_profile_raw (internal_uuid);
CREATE INDEX idx_cust_raw_extracted_at ON bronze.customer_profile_raw (extracted_at);
