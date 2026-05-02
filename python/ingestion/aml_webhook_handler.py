"""
AML / PEP Screening Webhook Handler
Source: webhook callbacks, matched on name + DOB
Exposes a FastAPI endpoint; persists payload to bronze.aml_raw.
Idempotent: duplicate event_ids are silently ignored.
"""

import logging
import uuid
from datetime import date, datetime, timezone

import psycopg2
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from psycopg2.extras import Json
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)
app = FastAPI(title="AML Webhook Receiver")

# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class AMLWebhookPayload(BaseModel):
    event_id: str                   # provider's unique event ID (idempotency key)
    customer_name: str
    date_of_birth: date
    is_pep: bool = False
    is_sanctioned: bool = False
    is_adverse_media: bool = False
    overall_risk_level: str
    match_details: list[dict] = []
    screening_timestamp: datetime | None = None

    @field_validator("customer_name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("customer_name must not be blank")
        return v.strip()

    @field_validator("overall_risk_level")
    @classmethod
    def valid_risk_level(cls, v: str) -> str:
        allowed = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"overall_risk_level must be one of {allowed}")
        return v.upper()


# ---------------------------------------------------------------------------
# Database helper (injected via app.state in production)
# ---------------------------------------------------------------------------

def _get_conn(request: Request) -> psycopg2.extensions.connection:
    return request.app.state.db_conn


def insert_aml_bronze(payload: AMLWebhookPayload, raw_body: dict, conn) -> int | None:
    batch_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bronze.aml_raw
                (customer_name, date_of_birth, raw_payload, webhook_event_id,
                 webhook_received_at, provider_timestamp, batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (webhook_event_id) DO NOTHING
            RETURNING id
            """,
            (
                payload.customer_name,
                payload.date_of_birth,
                Json(raw_body),
                payload.event_id,
                datetime.now(tz=timezone.utc),
                payload.screening_timestamp,
                batch_id,
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None  # None means duplicate — already processed


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "replace-with-env-var"  # verified against X-Webhook-Secret header


@app.post("/webhooks/aml", status_code=status.HTTP_200_OK)
async def receive_aml_webhook(
    request: Request,
    x_webhook_secret: str = Header(alias="X-Webhook-Secret"),
):
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret")

    raw_body = await request.json()
    try:
        payload = AMLWebhookPayload(**raw_body)
    except Exception as exc:
        logger.error("AML webhook parse error: %s | body: %s", exc, raw_body)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    conn = _get_conn(request)
    bronze_id = insert_aml_bronze(payload, raw_body, conn)

    if bronze_id is None:
        logger.info("Duplicate AML event_id=%s — ignored", payload.event_id)
        return JSONResponse({"status": "duplicate", "event_id": payload.event_id})

    logger.info("AML webhook stored: event_id=%s, bronze_id=%d", payload.event_id, bronze_id)
    return JSONResponse({"status": "accepted", "event_id": payload.event_id, "bronze_id": bronze_id})


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}
