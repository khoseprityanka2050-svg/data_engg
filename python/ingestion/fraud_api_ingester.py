"""
Fraud Detection Provider Ingester
Source: REST API, JSON, real-time scoring, matched on phone + email
Lands records in bronze.fraud_raw. Called synchronously per application.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import psycopg2
from psycopg2.extras import Json

logger = logging.getLogger(__name__)

_FRAUD_BAND_THRESHOLDS = {
    "LOW":      (0.00, 0.30),
    "MEDIUM":   (0.30, 0.60),
    "HIGH":     (0.60, 0.85),
    "CRITICAL": (0.85, 1.01),
}


@dataclass
class FraudScoreRequest:
    phone: str
    email: str
    customer_name: str | None = None
    device_fingerprint: str | None = None
    ip_address: str | None = None


@dataclass
class FraudScoreResponse:
    fraud_score: float
    fraud_band: str
    identity_verified: bool
    velocity_flags: dict
    device_risk_score: float | None
    ip_risk_score: float | None
    model_version: str
    provider_request_id: str
    raw_response: dict
    http_status: int
    latency_ms: int


def _score_to_band(score: float) -> str:
    for band, (low, high) in _FRAUD_BAND_THRESHOLDS.items():
        if low <= score < high:
            return band
    return "CRITICAL"


class FraudAPIClient:
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 5.0, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._client = httpx.Client(
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            timeout=timeout_s,
        )

    def score(self, request: FraudScoreRequest) -> FraudScoreResponse:
        payload = {
            "phone": request.phone,
            "email": request.email,
            **({"name": request.customer_name} if request.customer_name else {}),
            **({"device_fingerprint": request.device_fingerprint} if request.device_fingerprint else {}),
            **({"ip_address": request.ip_address} if request.ip_address else {}),
        }

        for attempt in range(1, self.max_retries + 1):
            t0 = time.monotonic()
            try:
                resp = self._client.post(f"{self.base_url}/v1/score", json=payload)
                latency_ms = int((time.monotonic() - t0) * 1000)
                resp.raise_for_status()
                data = resp.json()
                fraud_score = float(data["fraud_score"])
                return FraudScoreResponse(
                    fraud_score=fraud_score,
                    fraud_band=_score_to_band(fraud_score),
                    identity_verified=data.get("identity_verified", False),
                    velocity_flags=data.get("velocity_flags", {}),
                    device_risk_score=data.get("device_risk_score"),
                    ip_risk_score=data.get("ip_risk_score"),
                    model_version=data.get("model_version", "unknown"),
                    provider_request_id=data.get("request_id", ""),
                    raw_response=data,
                    http_status=resp.status_code,
                    latency_ms=latency_ms,
                )
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                logger.warning("Fraud API attempt %d/%d failed: %s", attempt, self.max_retries, exc)
                if attempt == self.max_retries:
                    raise
                time.sleep(0.5 * attempt)  # linear backoff

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def insert_fraud_bronze(
    request: FraudScoreRequest,
    response: FraudScoreResponse,
    batch_id: str,
    conn,
) -> int:
    requested_at = datetime.now(tz=timezone.utc)
    responded_at = datetime.now(tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bronze.fraud_raw
                (request_phone, request_email, raw_response, http_status_code,
                 api_request_id, requested_at, responded_at, latency_ms, batch_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                request.phone,
                request.email,
                Json(response.raw_response),
                response.http_status,
                response.provider_request_id,
                requested_at,
                responded_at,
                response.latency_ms,
                batch_id,
            ),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


def score_and_store(
    request: FraudScoreRequest,
    db_conn,
    fraud_client: FraudAPIClient,
    batch_id: str | None = None,
) -> tuple[FraudScoreResponse, int]:
    """Score a single applicant and persist the result to bronze."""
    if batch_id is None:
        batch_id = str(uuid.uuid4())
    response = fraud_client.score(request)
    bronze_id = insert_fraud_bronze(request, response, batch_id, db_conn)
    return response, bronze_id
