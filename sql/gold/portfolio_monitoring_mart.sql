-- =============================================================================
-- GOLD LAYER: Portfolio Monitoring Mart — Risk Team Dashboards
-- Refreshed daily. Aggregated from gold.credit_decision_inputs + outcomes.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Daily portfolio snapshot (one row per product per day)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold.portfolio_daily_snapshot (
    snapshot_date           DATE         NOT NULL,
    product_type            VARCHAR(32)  NOT NULL,

    -- Volume
    total_applications      INTEGER      NOT NULL DEFAULT 0,
    total_approvals         INTEGER      NOT NULL DEFAULT 0,
    total_declines          INTEGER      NOT NULL DEFAULT 0,
    total_referred          INTEGER      NOT NULL DEFAULT 0,
    approval_rate           NUMERIC(5,4),

    -- Credit quality distribution
    avg_credit_score        NUMERIC(6,2),
    pct_score_below_600     NUMERIC(5,4),
    pct_score_600_700       NUMERIC(5,4),
    pct_score_above_700     NUMERIC(5,4),

    -- Fraud distribution
    avg_fraud_score         NUMERIC(5,4),
    pct_fraud_high_critical NUMERIC(5,4),

    -- AML flags
    pct_pep                 NUMERIC(5,4),
    pct_sanctioned          NUMERIC(5,4),

    -- Risk concentration
    avg_dti_ratio           NUMERIC(5,4),
    pct_dti_above_50        NUMERIC(5,4),
    avg_requested_amount_aed NUMERIC(12,2),

    -- Data quality
    avg_dq_score            NUMERIC(5,4),
    pct_low_dq              NUMERIC(5,4),   -- decisions with dq_score < 0.7

    PRIMARY KEY (snapshot_date, product_type)
);

-- ---------------------------------------------------------------------------
-- Procedure: refresh today's snapshot
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE gold.refresh_portfolio_snapshot(p_date DATE DEFAULT CURRENT_DATE)
LANGUAGE plpgsql
AS $$
BEGIN
    DELETE FROM gold.portfolio_daily_snapshot
    WHERE  snapshot_date = p_date;

    INSERT INTO gold.portfolio_daily_snapshot
    SELECT
        p_date                                                          AS snapshot_date,
        product_type,
        COUNT(*)                                                        AS total_applications,
        SUM(CASE WHEN decision_outcome = 'APPROVED'  THEN 1 ELSE 0 END) AS total_approvals,
        SUM(CASE WHEN decision_outcome = 'DECLINED'  THEN 1 ELSE 0 END) AS total_declines,
        SUM(CASE WHEN decision_outcome = 'REFERRED'  THEN 1 ELSE 0 END) AS total_referred,
        ROUND(
            SUM(CASE WHEN decision_outcome = 'APPROVED' THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0),
            4
        )                                                               AS approval_rate,

        -- Credit score distribution
        ROUND(AVG(aecb_credit_score),2)                                 AS avg_credit_score,
        ROUND(SUM(CASE WHEN aecb_credit_score < 600 THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0),4)
                                                                        AS pct_score_below_600,
        ROUND(SUM(CASE WHEN aecb_credit_score BETWEEN 600 AND 699 THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0),4)
                                                                        AS pct_score_600_700,
        ROUND(SUM(CASE WHEN aecb_credit_score >= 700 THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0),4)
                                                                        AS pct_score_above_700,

        -- Fraud
        ROUND(AVG(fraud_score),4)                                       AS avg_fraud_score,
        ROUND(SUM(CASE WHEN fraud_band IN ('HIGH','CRITICAL') THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0),4)
                                                                        AS pct_fraud_high_critical,

        -- AML
        ROUND(SUM(CASE WHEN aml_is_pep        THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0),4) AS pct_pep,
        ROUND(SUM(CASE WHEN aml_is_sanctioned  THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0),4) AS pct_sanctioned,

        -- Risk
        ROUND(AVG(dti_ratio),4)                                         AS avg_dti_ratio,
        ROUND(SUM(CASE WHEN dti_ratio > 0.5 THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0),4)
                                                                        AS pct_dti_above_50,
        ROUND(AVG(requested_amount_aed),2)                              AS avg_requested_amount_aed,

        -- Data quality
        ROUND(AVG(dq_score),4)                                          AS avg_dq_score,
        ROUND(SUM(CASE WHEN dq_score < 0.7 THEN 1 ELSE 0 END)::NUMERIC / NULLIF(COUNT(*),0),4)
                                                                        AS pct_low_dq

    FROM gold.credit_decision_inputs
    WHERE DATE(snapshot_created_at) = p_date
    GROUP BY product_type;
END;
$$;

-- ---------------------------------------------------------------------------
-- Risk concentration view: top delinquency cohorts
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.v_risk_concentration AS
SELECT
    product_type,
    CASE
        WHEN aecb_credit_score < 550  THEN 'Deep Subprime (<550)'
        WHEN aecb_credit_score < 620  THEN 'Subprime (550-619)'
        WHEN aecb_credit_score < 680  THEN 'Near Prime (620-679)'
        WHEN aecb_credit_score < 740  THEN 'Prime (680-739)'
        ELSE 'Super Prime (740+)'
    END                                                     AS score_band,
    CASE
        WHEN dti_ratio < 0.20 THEN 'Low DTI (<20%)'
        WHEN dti_ratio < 0.40 THEN 'Medium DTI (20-39%)'
        WHEN dti_ratio < 0.60 THEN 'High DTI (40-59%)'
        ELSE 'Very High DTI (60%+)'
    END                                                     AS dti_band,
    COUNT(*)                                                AS application_count,
    SUM(CASE WHEN decision_outcome = 'APPROVED' THEN 1 ELSE 0 END) AS approvals,
    ROUND(AVG(aecb_credit_score),1)                         AS avg_credit_score,
    ROUND(AVG(fraud_score),4)                               AS avg_fraud_score,
    SUM(CASE WHEN aml_is_pep OR aml_is_sanctioned THEN 1 ELSE 0 END) AS aml_flagged
FROM gold.credit_decision_inputs
WHERE decided_at >= NOW() - INTERVAL '30 days'
GROUP BY product_type, score_band, dti_band
ORDER BY product_type, application_count DESC;

-- ---------------------------------------------------------------------------
-- Trend view: 7-day rolling approval rate per product
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW gold.v_approval_rate_trend AS
SELECT
    snapshot_date,
    product_type,
    approval_rate,
    AVG(approval_rate) OVER (
        PARTITION BY product_type
        ORDER BY snapshot_date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    )                                           AS approval_rate_7d_avg,
    total_applications,
    avg_credit_score,
    avg_dti_ratio,
    pct_fraud_high_critical
FROM gold.portfolio_daily_snapshot
ORDER BY snapshot_date DESC, product_type;
