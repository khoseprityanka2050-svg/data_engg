-- =============================================================================
-- IDENTITY RESOLUTION: Match customers across 4 sources into silver.customer_master
-- Priority: internal_uuid > emirates_id > (phone + email) > (name + DOB fuzzy)
-- =============================================================================

-- Step 1: Seed from internal customer profiles (authoritative source)
INSERT INTO silver.customer_master (
    internal_uuid, emirates_id, phone_e164, email_normalised,
    full_name, date_of_birth, resolution_method, resolution_score
)
SELECT DISTINCT ON (cp.internal_uuid)
    (cp.raw_record->>'internal_uuid')::UUID                         AS internal_uuid,
    cp.raw_record->>'emirates_id'                                   AS emirates_id,
    cp.raw_record->>'phone_e164'                                    AS phone_e164,
    LOWER(TRIM(cp.raw_record->>'email'))                            AS email_normalised,
    TRIM(cp.raw_record->>'full_name')                               AS full_name,
    (cp.raw_record->>'date_of_birth')::DATE                         AS date_of_birth,
    'SEED_INTERNAL'                                                 AS resolution_method,
    1.0                                                             AS resolution_score
FROM bronze.customer_profile_raw cp
WHERE cp.is_processed = FALSE
ON CONFLICT (internal_uuid) DO UPDATE
    SET phone_e164       = EXCLUDED.phone_e164,
        email_normalised = EXCLUDED.email_normalised,
        full_name        = EXCLUDED.full_name,
        updated_at       = NOW();

-- Step 2: Link AECB data via Emirates ID (exact match — highest confidence)
UPDATE silver.customer_master cm
SET    emirates_id = ar.emirates_id,
       updated_at  = NOW(),
       resolution_method = CASE
           WHEN cm.resolution_method = 'SEED_INTERNAL' THEN 'SEED_INTERNAL+AECB_EMIRATES_ID'
           ELSE 'AECB_EMIRATES_ID'
       END
FROM (
    SELECT DISTINCT emirates_id
    FROM   bronze.aecb_raw
    WHERE  is_processed = FALSE
) ar
WHERE cm.emirates_id = ar.emirates_id;

-- Step 3: Link fraud scores via phone + email (AND condition — dual-key match)
WITH fraud_candidates AS (
    SELECT DISTINCT
        fr.request_phone                          AS phone_e164,
        LOWER(TRIM(fr.request_email))             AS email_normalised
    FROM   bronze.fraud_raw fr
    WHERE  fr.is_processed = FALSE
)
UPDATE silver.customer_master cm
SET    updated_at        = NOW(),
       resolution_method = resolution_method || '+FRAUD_PHONE_EMAIL'
FROM   fraud_candidates fc
WHERE  cm.phone_e164        = fc.phone_e164
  AND  cm.email_normalised  = fc.email_normalised;

-- Step 4: Link AML data — name + DOB (fuzzy name, exact DOB)
-- Uses pg_trgm similarity; threshold 0.85 is conservative for Arabic/English names
WITH aml_candidates AS (
    SELECT DISTINCT
        TRIM(customer_name)  AS customer_name,
        date_of_birth
    FROM   bronze.aml_raw
    WHERE  is_processed = FALSE
),
ranked_matches AS (
    SELECT
        cm.customer_key,
        ac.customer_name,
        ac.date_of_birth,
        similarity(cm.full_name, ac.customer_name) AS name_sim,
        ROW_NUMBER() OVER (
            PARTITION BY ac.customer_name, ac.date_of_birth
            ORDER BY similarity(cm.full_name, ac.customer_name) DESC
        ) AS rn
    FROM   silver.customer_master cm
    JOIN   aml_candidates ac
        ON cm.date_of_birth = ac.date_of_birth
       AND similarity(cm.full_name, ac.customer_name) >= 0.85
)
UPDATE silver.customer_master cm
SET    resolution_method = resolution_method || '+AML_NAME_DOB',
       updated_at        = NOW()
FROM   ranked_matches rm
WHERE  rm.rn = 1
  AND  cm.customer_key = rm.customer_key;

-- Step 5: Audit — unmatched AML records surfaced for manual review
CREATE TABLE IF NOT EXISTS silver.identity_resolution_exceptions (
    id              BIGSERIAL   PRIMARY KEY,
    source_system   VARCHAR(32) NOT NULL,
    source_key      TEXT        NOT NULL,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason          TEXT        NOT NULL,
    raw_payload     JSONB
);

INSERT INTO silver.identity_resolution_exceptions (source_system, source_key, reason, raw_payload)
SELECT
    'AML',
    customer_name || '|' || date_of_birth::TEXT,
    'No customer_master match on name+DOB with similarity >= 0.85',
    raw_payload
FROM bronze.aml_raw ar
WHERE ar.is_processed = FALSE
  AND NOT EXISTS (
      SELECT 1
      FROM   silver.customer_master cm
      WHERE  cm.date_of_birth = ar.date_of_birth
        AND  similarity(cm.full_name, ar.customer_name) >= 0.85
  );
