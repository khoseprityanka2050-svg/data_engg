"""
Identity resolution — matches customers across 4 source systems into one customer_key.

This is the trickiest piece of the whole pipeline. Each source identifies the same
customer differently:
  - Internal DB   → internal_uuid
  - AECB          → Emirates ID
  - Fraud vendor  → phone + email
  - AML provider  → name + date of birth

We resolve in priority order. If we can match on a reliable key (UUID, Emirates ID),
we use that. We only fall back to fuzzy name matching when nothing else works, and
even then we flag it for manual review.

The 0.85 threshold on name similarity came from testing on ~200 real name pairs.
Arabic names transliterated into English caused the most false negatives — "Mohammed"
vs "Mohammad" vs "Mohamed" all need to link to the same person. Going above 0.85
started missing legitimate matches. Going below introduced too many false positives.
It's a tradeoff we'll monitor and may adjust after 30 days of production data.

TODO: consider Soundex or a phonetic algorithm for Arabic names as an alternative
to pg_trgm — might be more accurate for transliteration variants.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Strip everything except digits and leading +
_PHONE_RE = re.compile(r"[^\d+]")


class ResolutionMethod(str, Enum):
    INTERNAL_UUID   = "INTERNAL_UUID"
    EMIRATES_ID     = "EMIRATES_ID"
    PHONE_EMAIL     = "PHONE_EMAIL"
    NAME_DOB_FUZZY  = "NAME_DOB_FUZZY"
    NO_MATCH        = "NO_MATCH"


@dataclass
class CustomerIdentity:
    """All the identity signals we might have for one customer."""
    internal_uuid:  Optional[str] = None
    emirates_id:    Optional[str] = None
    phone_raw:      Optional[str] = None   # whatever format the source gives us
    email_raw:      Optional[str] = None
    full_name:      Optional[str] = None
    date_of_birth:  Optional[str] = None   # ISO date string e.g. "1990-03-15"


@dataclass
class ResolutionResult:
    customer_key:   str
    method:         ResolutionMethod
    confidence:     float        # 0–1, used to flag low-confidence matches for review
    is_new_customer: bool
    conflicts:      list[str]    # data mismatches found during matching


def _normalise_phone(raw: str) -> str:
    """
    Normalise to E.164 format (+971XXXXXXXXX for UAE numbers).
    Sources give us numbers in wildly different formats:
      "050 123 4567", "00971501234567", "+971-50-123-4567" should all become "+971501234567"
    """
    digits = _PHONE_RE.sub("", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif not digits.startswith("+"):
        # assume UAE if no country code — might revisit if we get international applicants
        digits = "+971" + digits.lstrip("0")
    return digits


def _normalise_email(raw: str) -> str:
    return raw.strip().lower()


class IdentityResolver:

    # 0.85 chosen empirically — see module docstring
    FUZZY_NAME_THRESHOLD = 0.85

    def __init__(self, db_conn):
        self._conn = db_conn

    def resolve(self, identity: CustomerIdentity) -> ResolutionResult:
        """
        Try each matching tier in order. Return on first hit.
        If nothing matches, create a new customer record.
        """
        conflicts = []

        # Tier 1: internal UUID — should match for any returning customer
        if identity.internal_uuid:
            row = self._match_by_uuid(identity.internal_uuid)
            if row:
                self._check_conflicts(row["customer_key"], identity, conflicts)
                return ResolutionResult(
                    customer_key=row["customer_key"],
                    method=ResolutionMethod.INTERNAL_UUID,
                    confidence=1.0,
                    is_new_customer=False,
                    conflicts=conflicts,
                )

        # Tier 2: Emirates ID — government-issued, highly reliable
        if identity.emirates_id:
            row = self._match_by_emirates_id(identity.emirates_id)
            if row:
                self._check_conflicts(row["customer_key"], identity, conflicts)
                return ResolutionResult(
                    customer_key=row["customer_key"],
                    method=ResolutionMethod.EMIRATES_ID,
                    confidence=0.99,
                    is_new_customer=False,
                    conflicts=conflicts,
                )

        # Tier 3: phone + email — require both to reduce false positives
        # A shared phone (e.g. family plan) alone isn't reliable enough
        if identity.phone_raw and identity.email_raw:
            phone = _normalise_phone(identity.phone_raw)
            email = _normalise_email(identity.email_raw)
            row = self._match_by_phone_email(phone, email)
            if row:
                return ResolutionResult(
                    customer_key=row["customer_key"],
                    method=ResolutionMethod.PHONE_EMAIL,
                    confidence=0.90,
                    is_new_customer=False,
                    conflicts=conflicts,
                )

        # Tier 4: name + DOB fuzzy — last resort, always flags for review
        if identity.full_name and identity.date_of_birth:
            row = self._match_by_name_dob(identity.full_name, identity.date_of_birth)
            if row:
                return ResolutionResult(
                    customer_key=row["customer_key"],
                    method=ResolutionMethod.NAME_DOB_FUZZY,
                    confidence=float(row["name_similarity"]),
                    is_new_customer=False,
                    conflicts=["Fuzzy name+DOB match — needs manual verification"],
                )

        # No match on any tier — create a new customer record
        new_key = self._create_customer(identity)
        return ResolutionResult(
            customer_key=new_key,
            method=ResolutionMethod.NO_MATCH,
            confidence=1.0,
            is_new_customer=True,
            conflicts=[],
        )

    def _match_by_uuid(self, internal_uuid: str) -> Optional[dict]:
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT customer_key FROM silver.customer_master WHERE internal_uuid = %s",
                (internal_uuid,),
            )
            return cur.fetchone()

    def _match_by_emirates_id(self, emirates_id: str) -> Optional[dict]:
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT customer_key FROM silver.customer_master WHERE emirates_id = %s",
                (emirates_id,),
            )
            return cur.fetchone()

    def _match_by_phone_email(self, phone: str, email: str) -> Optional[dict]:
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT customer_key FROM silver.customer_master
                WHERE phone_e164 = %s AND email_normalised = %s
                """,
                (phone, email),
            )
            return cur.fetchone()

    def _match_by_name_dob(self, full_name: str, date_of_birth: str) -> Optional[dict]:
        """
        Uses PostgreSQL pg_trgm similarity function.
        Exact DOB match required to anchor the fuzzy name search —
        without it we'd get too many false matches on common names.
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT customer_key, full_name,
                       similarity(full_name, %s) AS name_similarity
                FROM silver.customer_master
                WHERE date_of_birth = %s::DATE
                  AND similarity(full_name, %s) >= %s
                ORDER BY name_similarity DESC
                LIMIT 1
                """,
                (full_name, date_of_birth, full_name, self.FUZZY_NAME_THRESHOLD),
            )
            return cur.fetchone()

    def _check_conflicts(self, customer_key: str, identity: CustomerIdentity, conflicts: list[str]):
        """
        Look for data mismatches between what we already have and what's coming in.
        We don't overwrite — we just log the conflict so someone can investigate.
        Silent overwrites are dangerous in a credit context.
        """
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT emirates_id, date_of_birth FROM silver.customer_master WHERE customer_key = %s",
                (customer_key,),
            )
            existing = cur.fetchone()
        if not existing:
            return

        if (identity.emirates_id and existing["emirates_id"]
                and existing["emirates_id"] != identity.emirates_id):
            conflicts.append(
                f"Emirates ID conflict: stored={existing['emirates_id']} vs incoming={identity.emirates_id}"
            )
        if identity.date_of_birth and existing["date_of_birth"]:
            if str(existing["date_of_birth"]) != identity.date_of_birth:
                conflicts.append(
                    f"DOB conflict: stored={existing['date_of_birth']} vs incoming={identity.date_of_birth}"
                )

    def _create_customer(self, identity: CustomerIdentity) -> str:
        phone = _normalise_phone(identity.phone_raw) if identity.phone_raw else None
        email = _normalise_email(identity.email_raw) if identity.email_raw else None
        key = str(uuid.uuid4())

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO silver.customer_master
                    (customer_key, internal_uuid, emirates_id, phone_e164, email_normalised,
                     full_name, date_of_birth, resolution_method, resolution_score)
                VALUES (%s, %s, %s, %s, %s, %s, %s::DATE, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    key,
                    identity.internal_uuid,
                    identity.emirates_id,
                    phone,
                    email,
                    identity.full_name,
                    identity.date_of_birth,
                    ResolutionMethod.NO_MATCH.value,
                    1.0,
                ),
            )
        self._conn.commit()
        logger.info("New customer created: customer_key=%s", key)
        return key
