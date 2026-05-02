-- =============================================================================
-- SILVER LAYER: Cleansed, parsed, standardised, entity-resolved
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Master entity resolution table
-- Each row = one confirmed unique customer across all source systems
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.customer_master (
    customer_key        UUID         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    internal_uuid       UUID,                           -- from customer_profile
    emirates_id         VARCHAR(18),                    -- from AECB
    phone_e164          VARCHAR(20),                    -- from fraud provider (normalised)
    email_normalised    VARCHAR(320),                   -- from fraud provider (lowercased)
    full_name           VARCHAR(512),                   -- canonical name
    date_of_birth       DATE,                           -- for AML matching
    resolution_method   VARCHAR(64)  NOT NULL,          -- see identity_resolver.py
    resolution_score    NUMERIC(5,4),                   -- confidence 0-1
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    is_active           BOOLEAN      NOT NULL DEFAULT TRUE
);

CREATE UNIQUE INDEX idx_cm_internal_uuid  ON silver.customer_master (internal_uuid) WHERE internal_uuid IS NOT NULL;
CREATE UNIQUE INDEX idx_cm_emirates_id    ON silver.customer_master (emirates_id)   WHERE emirates_id IS NOT NULL;
CREATE INDEX idx_cm_phone                ON silver.customer_master (phone_e164);
CREATE INDEX idx_cm_email                ON silver.customer_master (email_normalised);

-- ---------------------------------------------------------------------------
-- Parsed AECB credit bureau data
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.aecb_parsed (
    id                      BIGSERIAL    PRIMARY KEY,
    customer_key            UUID         NOT NULL REFERENCES silver.customer_master(customer_key),
    emirates_id             VARCHAR(18)  NOT NULL,
    credit_score            SMALLINT     CHECK (credit_score BETWEEN 300 AND 900),
    total_outstanding_aed   NUMERIC(15,2),
    total_credit_limit_aed  NUMERIC(15,2),
    utilisation_rate        NUMERIC(5,4),
    active_loan_count       SMALLINT,
    delinquency_30d_count   SMALLINT,
    delinquency_60d_count   SMALLINT,
    delinquency_90d_count   SMALLINT,
    worst_status_ever       VARCHAR(32),
    inquiry_count_6m        SMALLINT,
    earliest_account_date   DATE,
    report_date             DATE         NOT NULL,
    source_batch_id         UUID         NOT NULL,
    bronze_id               BIGINT       NOT NULL,      -- FK to bronze.aecb_raw.id
    parsed_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_aecb_parsed_ckey  ON silver.aecb_parsed (customer_key);
CREATE INDEX idx_aecb_parsed_date  ON silver.aecb_parsed (report_date);

-- ---------------------------------------------------------------------------
-- Parsed Fraud scores
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.fraud_scores (
    id                  BIGSERIAL    PRIMARY KEY,
    customer_key        UUID         NOT NULL REFERENCES silver.customer_master(customer_key),
    fraud_score         NUMERIC(5,4) NOT NULL CHECK (fraud_score BETWEEN 0 AND 1),
    fraud_band          VARCHAR(16)  NOT NULL CHECK (fraud_band IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    velocity_flags      JSONB,                          -- structured signal flags
    device_risk_score   NUMERIC(5,4),
    ip_risk_score       NUMERIC(5,4),
    identity_verified   BOOLEAN,
    score_model_version VARCHAR(32),
    scored_at           TIMESTAMPTZ  NOT NULL,
    source_batch_id     UUID         NOT NULL,
    bronze_id           BIGINT       NOT NULL,
    parsed_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_fraud_ckey       ON silver.fraud_scores (customer_key);
CREATE INDEX idx_fraud_scored_at  ON silver.fraud_scores (scored_at);

-- ---------------------------------------------------------------------------
-- Parsed AML / PEP results
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.aml_results (
    id                  BIGSERIAL    PRIMARY KEY,
    customer_key        UUID         NOT NULL REFERENCES silver.customer_master(customer_key),
    is_pep              BOOLEAN      NOT NULL DEFAULT FALSE,
    is_sanctioned       BOOLEAN      NOT NULL DEFAULT FALSE,
    is_adverse_media    BOOLEAN      NOT NULL DEFAULT FALSE,
    overall_risk_level  VARCHAR(16)  NOT NULL CHECK (overall_risk_level IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    match_details       JSONB,                          -- list of matched entities/lists
    screening_date      DATE         NOT NULL,
    next_review_date    DATE,
    source_batch_id     UUID         NOT NULL,
    bronze_id           BIGINT       NOT NULL,
    parsed_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_aml_ckey   ON silver.aml_results (customer_key);
CREATE INDEX idx_aml_date   ON silver.aml_results (screening_date);

-- ---------------------------------------------------------------------------
-- Normalised Customer Profiles
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.customer_profiles (
    id                      BIGSERIAL    PRIMARY KEY,
    customer_key            UUID         NOT NULL REFERENCES silver.customer_master(customer_key),
    internal_uuid           UUID         NOT NULL,
    full_name               VARCHAR(512) NOT NULL,
    date_of_birth           DATE         NOT NULL,
    nationality             CHAR(2),                   -- ISO 3166-1 alpha-2
    emirates_id             VARCHAR(18),
    phone_e164              VARCHAR(20)  NOT NULL,
    email                   VARCHAR(320) NOT NULL,
    monthly_income_aed      NUMERIC(12,2),
    employment_status       VARCHAR(32),
    employer_name           VARCHAR(256),
    years_employed          NUMERIC(4,1),
    residence_city          VARCHAR(128),
    profile_version         INTEGER      NOT NULL DEFAULT 1,
    source_updated_at       TIMESTAMPTZ  NOT NULL,
    source_batch_id         UUID         NOT NULL,
    bronze_id               BIGINT       NOT NULL,
    parsed_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_cust_profile_ckey  ON silver.customer_profiles (customer_key);
CREATE INDEX idx_cust_profile_uuid  ON silver.customer_profiles (internal_uuid);
