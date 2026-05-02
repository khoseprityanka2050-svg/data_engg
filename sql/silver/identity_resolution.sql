-- Identity resolution — links bronze records from 4 sources to silver.customer_master
--
-- Run order matters. We seed from our own customer DB first (most trusted source),
-- then progressively link the external sources using whatever keys they share.
--
-- Matching priority:
--   1. internal_uuid  — our own system, authoritative
--   2. emirates_id    — government-issued, exact match
--   3. phone + email  — both must match (one alone is too loose)
--   4. name + DOB     — fuzzy name, exact DOB — last resort, logged for review
--
-- Note: requires pg_trgm extension for similarity() in step 4.
-- Run: CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- -------------------------------------------------------------------------
-- Step 1: Seed customer_master from internal profile DB (the authoritative source)
--
-- We use DISTINCT ON (internal_uuid) in case CDC delivered multiple versions
-- of the same customer in one batch — we only want the most recent one.
-- ON CONFLICT DO UPDATE keeps phone/email/name fresh if the customer updated their profile.
-- -------------------------------------------------------------------------
INSERT INTO silver.customer_master (
    internal_uuid, emirates_id, phone_e164, email_normalised,
    full_name, date_of_birth, resolution_method, resolution_score
)
SELECT DISTINCT ON (cp.internal_uuid)
    (cp.raw_record->>'internal_uuid')::UUID             AS internal_uuid,
    cp.raw_record->>'emirates_id'                       AS emirates_id,
    cp.raw_record->>'phone_e164'                        AS phone_e164,
    LOWER(TRIM(cp.raw_record->>'email'))                AS email_normalised,
    TRIM(cp.raw_record->>'full_name')                   AS full_name,
    (cp.raw_record->>'date_of_birth')::DATE             AS date_of_birth,
    'SEED_INTERNAL'                                     AS resolution_method,
    1.0                                                 AS resolution_score
FROM bronze.customer_profile_raw cp
WHERE cp.is_processed = FALSE
ORDER BY cp.internal_uuid, cp.source_updated_at DESC
ON CONFLICT (internal_uuid) DO UPDATE
    SET phone_e164       = EXCLUDED.phone_e164,
        email_normalised = EXCLUDED.email_normalised,
        full_name        = EXCLUDED.full_name,
        updated_at       = NOW();


-- -------------------------------------------------------------------------
-- Step 2: Link AECB records via Emirates ID (exact match)
--
-- AECB is our most credit-specific source. If a customer_master row already
-- has an emirates_id (set in step 1), we update the resolution_method label
-- to show we've now confirmed it cross-source.
-- -------------------------------------------------------------------------
UPDATE silver.customer_master cm
SET
    emirates_id       = ar.emirates_id,
    updated_at        = NOW(),
    resolution_method = CASE
        WHEN cm.resolution_method = 'SEED_INTERNAL' THEN 'SEED_INTERNAL+AECB'
        ELSE 'AECB_EMIRATES_ID'
    END
FROM (
    SELECT DISTINCT emirates_id
    FROM   bronze.aecb_raw
    WHERE  is_processed = FALSE
) ar
WHERE cm.emirates_id = ar.emirates_id;


-- -------------------------------------------------------------------------
-- Step 3: Link fraud scores via phone + email (both fields must match)
--
-- We require BOTH phone AND email to match. Phone alone isn't safe — family
-- members share phones. Email alone isn't safe — people reuse emails.
-- Together they're reliable enough to link at medium confidence.
-- -------------------------------------------------------------------------
WITH fraud_candidates AS (
    SELECT DISTINCT
        request_phone                      AS phone_e164,
        LOWER(TRIM(request_email))         AS email_normalised
    FROM bronze.fraud_raw
    WHERE is_processed = FALSE
)
UPDATE silver.customer_master cm
SET
    updated_at        = NOW(),
    resolution_method = resolution_method || '+FRAUD'
FROM fraud_candidates fc
WHERE cm.phone_e164       = fc.phone_e164
  AND cm.email_normalised = fc.email_normalised;


-- -------------------------------------------------------------------------
-- Step 4: Link AML records via name similarity + exact DOB
--
-- This is the fuzzy step. pg_trgm's similarity() gives us a 0-1 score for
-- how similar two strings are. 0.85 was chosen after testing on ~200 name pairs.
-- The DOB exact-match anchors the search so we're not just matching on name alone.
--
-- ROW_NUMBER() ensures each AML record links to at most one customer_master row
-- (the closest match). Without this, a common name like "Ahmed Ali" on the same
-- DOB could match multiple customers.
-- -------------------------------------------------------------------------
WITH aml_candidates AS (
    SELECT DISTINCT
        TRIM(customer_name)  AS customer_name,
        date_of_birth
    FROM bronze.aml_raw
    WHERE is_processed = FALSE
),
best_matches AS (
    SELECT
        cm.customer_key,
        ac.customer_name,
        ac.date_of_birth,
        similarity(cm.full_name, ac.customer_name) AS name_sim,
        ROW_NUMBER() OVER (
            PARTITION BY ac.customer_name, ac.date_of_birth
            ORDER BY similarity(cm.full_name, ac.customer_name) DESC
        ) AS rn
    FROM silver.customer_master cm
    JOIN aml_candidates ac
        ON  cm.date_of_birth = ac.date_of_birth
        AND similarity(cm.full_name, ac.customer_name) >= 0.85
)
UPDATE silver.customer_master cm
SET
    resolution_method = resolution_method || '+AML_NAME_DOB',
    updated_at        = NOW()
FROM best_matches bm
WHERE bm.rn = 1
  AND cm.customer_key = bm.customer_key;


-- -------------------------------------------------------------------------
-- Step 5: Log AML records we couldn't match — for manual review
--
-- Any AML record that didn't get linked in step 4 is an exception.
-- These go into a separate table and get reviewed weekly.
-- Common causes: name badly transliterated, DOB mismatch between sources,
-- or a genuinely new customer that AML screened before we onboarded them.
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS silver.identity_resolution_exceptions (
    id            BIGSERIAL    PRIMARY KEY,
    source_system VARCHAR(32)  NOT NULL,
    source_key    TEXT         NOT NULL,   -- e.g. "Ahmed Ali|1985-04-10"
    attempted_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    reason        TEXT         NOT NULL,
    raw_payload   JSONB
);

INSERT INTO silver.identity_resolution_exceptions
    (source_system, source_key, reason, raw_payload)
SELECT
    'AML',
    ar.customer_name || '|' || ar.date_of_birth::TEXT,
    'No match found — name similarity below 0.85 or DOB mismatch',
    ar.raw_payload
FROM bronze.aml_raw ar
WHERE ar.is_processed = FALSE
  AND NOT EXISTS (
      SELECT 1
      FROM   silver.customer_master cm
      WHERE  cm.date_of_birth = ar.date_of_birth
        AND  similarity(cm.full_name, ar.customer_name) >= 0.85
  );
