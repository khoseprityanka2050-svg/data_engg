-- =============================================================================
-- GOLD LAYER: Immutable Credit Decision Input Snapshots
-- One row per credit decision; captures point-in-time state of ALL inputs.
-- Append-only. Never updated after insert. Required for CBUAE audit trail.
-- =============================================================================

CREATE TABLE IF NOT EXISTS gold.credit_decision_inputs (
    -- Identity
    decision_id             UUID         NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    customer_key            UUID         NOT NULL,
    product_type            VARCHAR(32)  NOT NULL
        CHECK (product_type IN ('personal_finance','bnpl','credit_card_alternative')),
    application_id          UUID         NOT NULL,

    -- Snapshot timestamps (when each source datum was valid)
    snapshot_created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    aecb_report_date        DATE,
    fraud_scored_at         TIMESTAMPTZ,
    aml_screening_date      DATE,
    profile_as_of           TIMESTAMPTZ,

    -- AECB inputs (point-in-time)
    aecb_credit_score               SMALLINT,
    aecb_utilisation_rate           NUMERIC(5,4),
    aecb_delinquency_30d            SMALLINT,
    aecb_delinquency_60d            SMALLINT,
    aecb_delinquency_90d            SMALLINT,
    aecb_active_loan_count          SMALLINT,
    aecb_worst_status_ever          VARCHAR(32),
    aecb_total_outstanding_aed      NUMERIC(15,2),
    aecb_inquiry_count_6m           SMALLINT,

    -- Fraud inputs
    fraud_score                     NUMERIC(5,4),
    fraud_band                      VARCHAR(16),
    fraud_identity_verified         BOOLEAN,
    fraud_velocity_flags            JSONB,

    -- AML inputs
    aml_is_pep                      BOOLEAN,
    aml_is_sanctioned               BOOLEAN,
    aml_is_adverse_media            BOOLEAN,
    aml_overall_risk_level          VARCHAR(16),

    -- Customer profile inputs
    applicant_monthly_income_aed    NUMERIC(12,2),
    applicant_employment_status     VARCHAR(32),
    applicant_years_employed        NUMERIC(4,1),
    applicant_nationality           CHAR(2),

    -- Derived ratios (computed at snapshot time, not recalculated later)
    dti_ratio                       NUMERIC(5,4),       -- debt-to-income
    requested_amount_aed            NUMERIC(12,2),
    requested_tenor_months          SMALLINT,

    -- Data quality at snapshot time
    dq_score                        NUMERIC(5,4),       -- 0-1, see dq_scorecard
    dq_flags                        JSONB,              -- list of failed rules

    -- Decision outcome (populated by decisioning engine, not ETL)
    decision_outcome                VARCHAR(16)
        CHECK (decision_outcome IN ('APPROVED','DECLINED','REFERRED','PENDING')),
    decision_reason_codes           TEXT[],
    decided_at                      TIMESTAMPTZ,
    decided_by                      VARCHAR(256),       -- model version or analyst ID

    -- Source traceability (FK pointers back to silver)
    silver_aecb_id                  BIGINT,
    silver_fraud_id                 BIGINT,
    silver_aml_id                   BIGINT,
    silver_profile_id               BIGINT,

    CONSTRAINT no_update_after_decision
        CHECK (decided_at IS NULL OR snapshot_created_at <= decided_at)
);

CREATE INDEX idx_cdi_customer_key   ON gold.credit_decision_inputs (customer_key);
CREATE INDEX idx_cdi_application_id ON gold.credit_decision_inputs (application_id);
CREATE INDEX idx_cdi_product_type   ON gold.credit_decision_inputs (product_type);
CREATE INDEX idx_cdi_snapshot_at    ON gold.credit_decision_inputs (snapshot_created_at);
CREATE INDEX idx_cdi_outcome        ON gold.credit_decision_inputs (decision_outcome);

-- ---------------------------------------------------------------------------
-- View: latest decision per application (convenience, not for audit)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.v_latest_decisions AS
SELECT DISTINCT ON (application_id)
    decision_id,
    customer_key,
    application_id,
    product_type,
    decision_outcome,
    decided_at,
    dq_score,
    aecb_credit_score,
    fraud_band,
    aml_overall_risk_level,
    dti_ratio,
    snapshot_created_at
FROM gold.credit_decision_inputs
ORDER BY application_id, snapshot_created_at DESC;

-- ---------------------------------------------------------------------------
-- Populate snapshot from silver (called per-application by ETL)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION gold.create_decision_snapshot(
    p_customer_key  UUID,
    p_application_id UUID,
    p_product_type  VARCHAR(32),
    p_requested_amount_aed NUMERIC(12,2),
    p_requested_tenor_months SMALLINT
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    v_decision_id UUID;
    v_aecb        silver.aecb_parsed%ROWTYPE;
    v_fraud       silver.fraud_scores%ROWTYPE;
    v_aml         silver.aml_results%ROWTYPE;
    v_profile     silver.customer_profiles%ROWTYPE;
    v_dti         NUMERIC(5,4);
BEGIN
    -- Fetch latest record from each silver table
    SELECT * INTO v_aecb
    FROM silver.aecb_parsed
    WHERE customer_key = p_customer_key
    ORDER BY report_date DESC LIMIT 1;

    SELECT * INTO v_fraud
    FROM silver.fraud_scores
    WHERE customer_key = p_customer_key
    ORDER BY scored_at DESC LIMIT 1;

    SELECT * INTO v_aml
    FROM silver.aml_results
    WHERE customer_key = p_customer_key
    ORDER BY screening_date DESC LIMIT 1;

    SELECT * INTO v_profile
    FROM silver.customer_profiles
    WHERE customer_key = p_customer_key
    ORDER BY source_updated_at DESC LIMIT 1;

    -- Compute DTI (monthly instalment / monthly income)
    IF v_profile.monthly_income_aed > 0 AND p_requested_tenor_months > 0 THEN
        v_dti := ROUND(
            (p_requested_amount_aed / p_requested_tenor_months) / v_profile.monthly_income_aed,
            4
        );
    END IF;

    INSERT INTO gold.credit_decision_inputs (
        customer_key, product_type, application_id,
        aecb_report_date,        fraud_scored_at,       aml_screening_date,   profile_as_of,
        aecb_credit_score,       aecb_utilisation_rate, aecb_delinquency_30d,
        aecb_delinquency_60d,    aecb_delinquency_90d,  aecb_active_loan_count,
        aecb_worst_status_ever,  aecb_total_outstanding_aed, aecb_inquiry_count_6m,
        fraud_score,             fraud_band,            fraud_identity_verified, fraud_velocity_flags,
        aml_is_pep,              aml_is_sanctioned,     aml_is_adverse_media,  aml_overall_risk_level,
        applicant_monthly_income_aed, applicant_employment_status,
        applicant_years_employed, applicant_nationality,
        dti_ratio, requested_amount_aed, requested_tenor_months,
        silver_aecb_id, silver_fraud_id, silver_aml_id, silver_profile_id
    ) VALUES (
        p_customer_key, p_product_type, p_application_id,
        v_aecb.report_date,         v_fraud.scored_at,          v_aml.screening_date, v_profile.source_updated_at,
        v_aecb.credit_score,        v_aecb.utilisation_rate,    v_aecb.delinquency_30d_count,
        v_aecb.delinquency_60d_count, v_aecb.delinquency_90d_count, v_aecb.active_loan_count,
        v_aecb.worst_status_ever,   v_aecb.total_outstanding_aed, v_aecb.inquiry_count_6m,
        v_fraud.fraud_score,        v_fraud.fraud_band,         v_fraud.identity_verified, v_fraud.velocity_flags,
        v_aml.is_pep,               v_aml.is_sanctioned,        v_aml.is_adverse_media, v_aml.overall_risk_level,
        v_profile.monthly_income_aed, v_profile.employment_status,
        v_profile.years_employed,   v_profile.nationality,
        v_dti, p_requested_amount_aed, p_requested_tenor_months,
        v_aecb.id, v_fraud.id, v_aml.id, v_profile.id
    )
    RETURNING decision_id INTO v_decision_id;

    RETURN v_decision_id;
END;
$$;
