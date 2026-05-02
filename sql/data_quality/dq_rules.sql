-- =============================================================================
-- DATA QUALITY RULES: Must-pass rules and alert thresholds
-- Executed after each silver-layer load. Results written to gold.dq_scorecard.
-- =============================================================================

CREATE TABLE IF NOT EXISTS gold.dq_scorecard (
    id              BIGSERIAL    PRIMARY KEY,
    run_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
    run_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    layer           VARCHAR(16)  NOT NULL CHECK (layer IN ('bronze','silver','gold')),
    source          VARCHAR(64)  NOT NULL,
    rule_id         VARCHAR(64)  NOT NULL,
    rule_name       VARCHAR(256) NOT NULL,
    severity        VARCHAR(16)  NOT NULL CHECK (severity IN ('MUST_PASS','HIGH','MEDIUM','LOW')),
    total_records   BIGINT       NOT NULL,
    passing_records BIGINT       NOT NULL,
    failing_records BIGINT       NOT NULL,
    pass_rate       NUMERIC(5,4) NOT NULL,
    threshold       NUMERIC(5,4) NOT NULL,   -- minimum acceptable pass_rate
    status          VARCHAR(16)  NOT NULL
        GENERATED ALWAYS AS (
            CASE WHEN pass_rate >= threshold THEN 'PASS' ELSE 'FAIL' END
        ) STORED,
    sample_failures JSONB                    -- up to 10 failing row PKs
);

CREATE INDEX idx_dq_run_id  ON gold.dq_scorecard (run_id);
CREATE INDEX idx_dq_source  ON gold.dq_scorecard (source, rule_id);
CREATE INDEX idx_dq_status  ON gold.dq_scorecard (status);

-- ---------------------------------------------------------------------------
-- RULE EVALUATIONS — run as part of the silver→gold ETL
-- ---------------------------------------------------------------------------

-- RULE DQ-AECB-01: Emirates ID must be 15-18 digits
INSERT INTO gold.dq_scorecard (layer, source, rule_id, rule_name, severity, total_records, passing_records, failing_records, pass_rate, threshold, sample_failures)
SELECT
    'silver', 'aecb_parsed', 'DQ-AECB-01',
    'Emirates ID format: 15-18 numeric characters',
    'MUST_PASS',
    COUNT(*),
    SUM(CASE WHEN emirates_id ~ '^\d{15,18}$' THEN 1 ELSE 0 END),
    SUM(CASE WHEN emirates_id !~ '^\d{15,18}$' THEN 1 ELSE 0 END),
    ROUND(SUM(CASE WHEN emirates_id ~ '^\d{15,18}$' THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0), 4),
    1.00,
    (SELECT jsonb_agg(id) FROM (
        SELECT id FROM silver.aecb_parsed WHERE emirates_id !~ '^\d{15,18}$' LIMIT 10
    ) s)
FROM silver.aecb_parsed;

-- RULE DQ-AECB-02: Credit score within AECB range 300-900
INSERT INTO gold.dq_scorecard (layer, source, rule_id, rule_name, severity, total_records, passing_records, failing_records, pass_rate, threshold, sample_failures)
SELECT
    'silver', 'aecb_parsed', 'DQ-AECB-02',
    'Credit score within valid range (300-900)',
    'MUST_PASS',
    COUNT(*),
    SUM(CASE WHEN credit_score BETWEEN 300 AND 900 THEN 1 ELSE 0 END),
    SUM(CASE WHEN credit_score NOT BETWEEN 300 AND 900 THEN 1 ELSE 0 END),
    ROUND(SUM(CASE WHEN credit_score BETWEEN 300 AND 900 THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0), 4),
    1.00,
    (SELECT jsonb_agg(id) FROM (
        SELECT id FROM silver.aecb_parsed WHERE credit_score NOT BETWEEN 300 AND 900 LIMIT 10
    ) s)
FROM silver.aecb_parsed;

-- RULE DQ-AECB-03: Report date not older than 90 days
INSERT INTO gold.dq_scorecard (layer, source, rule_id, rule_name, severity, total_records, passing_records, failing_records, pass_rate, threshold, sample_failures)
SELECT
    'silver', 'aecb_parsed', 'DQ-AECB-03',
    'AECB report is not stale (within 90 days)',
    'HIGH',
    COUNT(*),
    SUM(CASE WHEN report_date >= CURRENT_DATE - INTERVAL '90 days' THEN 1 ELSE 0 END),
    SUM(CASE WHEN report_date < CURRENT_DATE - INTERVAL '90 days' THEN 1 ELSE 0 END),
    ROUND(SUM(CASE WHEN report_date >= CURRENT_DATE - INTERVAL '90 days' THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0), 4),
    0.95,
    (SELECT jsonb_agg(id) FROM (
        SELECT id FROM silver.aecb_parsed WHERE report_date < CURRENT_DATE - INTERVAL '90 days' LIMIT 10
    ) s)
FROM silver.aecb_parsed;

-- RULE DQ-FRAUD-01: Fraud score in range [0,1]
INSERT INTO gold.dq_scorecard (layer, source, rule_id, rule_name, severity, total_records, passing_records, failing_records, pass_rate, threshold, sample_failures)
SELECT
    'silver', 'fraud_scores', 'DQ-FRAUD-01',
    'Fraud score within valid range (0-1)',
    'MUST_PASS',
    COUNT(*),
    SUM(CASE WHEN fraud_score BETWEEN 0 AND 1 THEN 1 ELSE 0 END),
    SUM(CASE WHEN fraud_score NOT BETWEEN 0 AND 1 THEN 1 ELSE 0 END),
    ROUND(SUM(CASE WHEN fraud_score BETWEEN 0 AND 1 THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0), 4),
    1.00,
    (SELECT jsonb_agg(id) FROM (
        SELECT id FROM silver.fraud_scores WHERE fraud_score NOT BETWEEN 0 AND 1 LIMIT 10
    ) s)
FROM silver.fraud_scores;

-- RULE DQ-FRAUD-02: Fraud score freshness — scored within last 24 hours
INSERT INTO gold.dq_scorecard (layer, source, rule_id, rule_name, severity, total_records, passing_records, failing_records, pass_rate, threshold, sample_failures)
SELECT
    'silver', 'fraud_scores', 'DQ-FRAUD-02',
    'Fraud score fresher than 24 hours',
    'HIGH',
    COUNT(*),
    SUM(CASE WHEN scored_at >= NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END),
    SUM(CASE WHEN scored_at < NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END),
    ROUND(SUM(CASE WHEN scored_at >= NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0), 4),
    0.99,
    NULL
FROM silver.fraud_scores;

-- RULE DQ-AML-01: No NULL risk level
INSERT INTO gold.dq_scorecard (layer, source, rule_id, rule_name, severity, total_records, passing_records, failing_records, pass_rate, threshold, sample_failures)
SELECT
    'silver', 'aml_results', 'DQ-AML-01',
    'AML overall_risk_level is not null',
    'MUST_PASS',
    COUNT(*),
    SUM(CASE WHEN overall_risk_level IS NOT NULL THEN 1 ELSE 0 END),
    SUM(CASE WHEN overall_risk_level IS NULL THEN 1 ELSE 0 END),
    ROUND(SUM(CASE WHEN overall_risk_level IS NOT NULL THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0), 4),
    1.00,
    NULL
FROM silver.aml_results;

-- RULE DQ-CUST-01: Monthly income > 0 for all approved applications
INSERT INTO gold.dq_scorecard (layer, source, rule_id, rule_name, severity, total_records, passing_records, failing_records, pass_rate, threshold, sample_failures)
SELECT
    'silver', 'customer_profiles', 'DQ-CUST-01',
    'Monthly income is positive',
    'HIGH',
    COUNT(*),
    SUM(CASE WHEN monthly_income_aed > 0 THEN 1 ELSE 0 END),
    SUM(CASE WHEN monthly_income_aed IS NULL OR monthly_income_aed <= 0 THEN 1 ELSE 0 END),
    ROUND(SUM(CASE WHEN monthly_income_aed > 0 THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0), 4),
    0.98,
    (SELECT jsonb_agg(id) FROM (
        SELECT id FROM silver.customer_profiles WHERE monthly_income_aed IS NULL OR monthly_income_aed <= 0 LIMIT 10
    ) s)
FROM silver.customer_profiles;

-- RULE DQ-GOLD-01: All 4 sources present for approved decisions
INSERT INTO gold.dq_scorecard (layer, source, rule_id, rule_name, severity, total_records, passing_records, failing_records, pass_rate, threshold, sample_failures)
SELECT
    'gold', 'credit_decision_inputs', 'DQ-GOLD-01',
    'All 4 source IDs present for approved decisions',
    'MUST_PASS',
    COUNT(*),
    SUM(CASE WHEN silver_aecb_id IS NOT NULL AND silver_fraud_id IS NOT NULL
             AND silver_aml_id IS NOT NULL AND silver_profile_id IS NOT NULL
             THEN 1 ELSE 0 END),
    SUM(CASE WHEN silver_aecb_id IS NULL OR silver_fraud_id IS NULL
             OR silver_aml_id IS NULL OR silver_profile_id IS NULL
             THEN 1 ELSE 0 END),
    ROUND(
        SUM(CASE WHEN silver_aecb_id IS NOT NULL AND silver_fraud_id IS NOT NULL
                 AND silver_aml_id IS NOT NULL AND silver_profile_id IS NOT NULL
                 THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0), 4),
    1.00,
    NULL
FROM gold.credit_decision_inputs
WHERE decision_outcome = 'APPROVED';

-- ---------------------------------------------------------------------------
-- Alert view: failing MUST_PASS or HIGH rules
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.v_dq_alerts AS
SELECT
    run_at,
    source,
    rule_id,
    rule_name,
    severity,
    pass_rate,
    threshold,
    failing_records,
    sample_failures
FROM gold.dq_scorecard
WHERE status = 'FAIL'
  AND severity IN ('MUST_PASS', 'HIGH')
ORDER BY run_at DESC, severity, source;
