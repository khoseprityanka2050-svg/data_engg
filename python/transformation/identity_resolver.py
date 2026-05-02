"""
Identity Resolution Engine
Matches customers across 4 source systems into a unified customer_key.
Resolution priority:
  1. internal_uuid (authoritative)
  2. emirates_id (exact, high confidence)
  3. phone_e164 + email (dual-key, medium confidence)
  4. full_name + DOB (fuzzy, lowest confidence — flags for review)
"""

import logging
import re
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from typing import Optional

import psycopg2

logger = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"[^\d+]")


class ResolutionMethod(str, Enum):
    INTERNAL_UUID       = "INTERNAL_UUID"
    EMIRATES_ID         = "EMIRATES_ID"
    PHONE_EMAIL         = "PHONE_EMAIL"
    NAME_DOB_FUZZY      = "NAME_DOB_FUZZY"
    NO_MATCH            = "NO_MATCH"


@dataclass
class CustomerIdentity:
    internal_uuid:      Optional[str] = None
    emirates_id:        Optional[str] = None
    phone_raw:          Optional[str] = None
    email_raw:          Optional[str] = None
    full_name:          Optional[str] = None
    date_of_birth:      Optional[str] = None    # ISO 8601 date string


@dataclass
class ResolutionResult:
    customer_key:       str
    method:             ResolutionMethod
    confidence:         float           # 0.0 – 1.0
    is_new_customer:    bool
    conflicts:          list[str]       # descriptions of data conflicts found


def _normalise_phone(raw: str) -> str:
    digits = _PHONE_RE.sub("", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif not digits.startswith("+"):
        digits = "+971" + digits.lstrip("0")  # default UAE country code
    return digits


def _normalise_email(raw: str) -> str:
    return raw.strip().lower()


def _name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


class IdentityResolver:
    FUZZY_NAME_THRESHOLD = 0.85

    def __init__(self, db_conn):
        self._conn = db_conn

    def resolve(self, identity: CustomerIdentity) -> ResolutionResult:
        """Return the customer_key for the given identity signals, creating one if needed."""
        conflicts: list[str] = []

        # --- Tier 1: internal UUID ---
        if identity.internal_uuid:
            result = self._match_by_internal_uuid(identity.internal_uuid)
            if result:
                self._check_conflicts(result["customer_key"], identity, conflicts)
                return ResolutionResult(
                    customer_key=result["customer_key"],
                    method=ResolutionMethod.INTERNAL_UUID,
                    confidence=1.0,
                    is_new_customer=False,
                    conflicts=conflicts,
                )

        # --- Tier 2: Emirates ID ---
        if identity.emirates_id:
            result = self._match_by_emirates_id(identity.emirates_id)
            if result:
                self._check_conflicts(result["customer_key"], identity, conflicts)
                return ResolutionResult(
                    customer_key=result["customer_key"],
                    method=ResolutionMethod.EMIRATES_ID,
                    confidence=0.99,
                    is_new_customer=False,
                    conflicts=conflicts,
                )

        # --- Tier 3: phone + email (both must match) ---
        if identity.phone_raw and identity.email_raw:
            phone = _normalise_phone(identity.phone_raw)
            email = _normalise_email(identity.email_raw)
            result = self._match_by_phone_email(phone, email)
            if result:
                return ResolutionResult(
                    customer_key=result["customer_key"],
                    method=ResolutionMethod.PHONE_EMAIL,
                    confidence=0.90,
                    is_new_customer=False,
                    conflicts=conflicts,
                )

        # --- Tier 4: name + DOB fuzzy ---
        if identity.full_name and identity.date_of_birth:
            result = self._match_by_name_dob(identity.full_name, identity.date_of_birth)
            if result:
                return ResolutionResult(
                    customer_key=result["customer_key"],
                    method=ResolutionMethod.NAME_DOB_FUZZY,
                    confidence=result["name_similarity"],
                    is_new_customer=False,
                    conflicts=["Name+DOB fuzzy match — verify manually"],
                )

        # --- No match: create new customer ---
        new_key = self._create_customer(identity)
        return ResolutionResult(
            customer_key=new_key,
            method=ResolutionMethod.NO_MATCH,
            confidence=1.0,
            is_new_customer=True,
            conflicts=[],
        )

    def _match_by_internal_uuid(self, internal_uuid: str) -> Optional[dict]:
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
        """Detect conflicting data on an already-matched customer."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM silver.customer_master WHERE customer_key = %s",
                (customer_key,),
            )
            existing = cur.fetchone()
        if not existing:
            return

        if identity.emirates_id and existing["emirates_id"] and existing["emirates_id"] != identity.emirates_id:
            conflicts.append(
                f"Emirates ID mismatch: stored={existing['emirates_id']} incoming={identity.emirates_id}"
            )
        if identity.date_of_birth and existing["date_of_birth"]:
            if str(existing["date_of_birth"]) != identity.date_of_birth:
                conflicts.append(
                    f"DOB mismatch: stored={existing['date_of_birth']} incoming={identity.date_of_birth}"
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
        logger.info("Created new customer_key=%s", key)
        return key
