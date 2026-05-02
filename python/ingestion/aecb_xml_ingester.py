"""
AECB XML ingester — reads daily batch from SFTP, lands into bronze.aecb_raw

AECB delivers one XML file per day via SFTP. Each file can contain thousands of
<CreditReport> elements. We pull each one out as a separate row so we can query
by Emirates ID without parsing XML every time.

Note: first version of this used xmltodict which was simpler, but AECB's namespace
declarations (http://www.aecb.gov.ae/schema/v2) kept breaking it on edge cases.
Switched to ElementTree with explicit namespace handling — more verbose but reliable.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator
from xml.etree import ElementTree as ET

import boto3
import paramiko
import psycopg2
from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)

# AECB XML namespace — confirmed from their schema doc (v2, updated Jan 2024)
AECB_NS = {"aecb": "http://www.aecb.gov.ae/schema/v2"}


@dataclass
class AECBFile:
    filename: str
    content: bytes
    received_at: datetime
    # checksum computed automatically so we never accidentally store wrong value
    checksum: str = field(init=False)

    def __post_init__(self):
        self.checksum = hashlib.sha256(self.content).hexdigest()


@dataclass
class AECBRecord:
    emirates_id: str
    raw_xml: str
    filename: str
    file_received_at: datetime
    checksum_sha256: str
    batch_id: str


class AECBSFTPClient:
    """
    Thin wrapper around paramiko SFTP.
    Tracks which files we've already processed using a marker file on the remote server
    (simpler than maintaining state in our DB and avoids double-processing on restarts).
    """

    def __init__(self, host: str, port: int, username: str, private_key_path: str, remote_dir: str):
        self.host = host
        self.port = port
        self.username = username
        self.private_key_path = private_key_path
        self.remote_dir = remote_dir
        self._client = None
        self._sftp = None

    def __enter__(self):
        key = paramiko.RSAKey.from_private_key_file(self.private_key_path)
        self._client = paramiko.SSHClient()
        # RejectPolicy not AutoAddPolicy — we want explicit host key verification
        self._client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self._client.connect(self.host, port=self.port, username=self.username, pkey=key)
        self._sftp = self._client.open_sftp()
        return self

    def __exit__(self, *_):
        if self._sftp:
            self._sftp.close()
        if self._client:
            self._client.close()

    def list_new_files(self, marker_prefix: str = "processed_") -> list[str]:
        """Return XML files that don't yet have a corresponding processed_ marker."""
        all_files = [f.filename for f in self._sftp.listdir_attr(self.remote_dir)
                     if f.filename.endswith(".xml")]
        done = {f.filename.removeprefix(marker_prefix)
                for f in self._sftp.listdir_attr(self.remote_dir)
                if f.filename.startswith(marker_prefix)}
        return [f for f in all_files if f not in done]

    def download_file(self, filename: str) -> AECBFile:
        remote_path = f"{self.remote_dir}/{filename}"
        with self._sftp.open(remote_path, "rb") as fh:
            content = fh.read()
        # use the file's mtime as received_at — more accurate than when we polled
        stat = self._sftp.stat(remote_path)
        received_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        return AECBFile(filename=filename, content=content, received_at=received_at)

    def mark_processed(self, filename: str):
        """Drop an empty marker file so we skip this on the next run."""
        marker_path = f"{self.remote_dir}/processed_{filename}"
        self._sftp.open(marker_path, "w").close()


def parse_aecb_xml(file: AECBFile, batch_id: str) -> Iterator[AECBRecord]:
    """
    Split a multi-customer AECB file into individual records.
    Each <CreditReport> element in the XML corresponds to one customer.
    We store the serialised subtree so silver parsing can re-parse just one customer's data.
    """
    root = ET.fromstring(file.content.decode("utf-8"))

    for report in root.findall(".//aecb:CreditReport", AECB_NS):
        emirates_id_el = report.find("aecb:EmiratesID", AECB_NS)
        if emirates_id_el is None or not emirates_id_el.text:
            # This shouldn't happen with valid AECB data, but log and skip rather than crash
            logger.warning("Skipping CreditReport with missing EmiratesID in %s", file.filename)
            continue

        yield AECBRecord(
            emirates_id=emirates_id_el.text.strip(),
            raw_xml=ET.tostring(report, encoding="unicode"),
            filename=file.filename,
            file_received_at=file.received_at,
            checksum_sha256=file.checksum,
            batch_id=batch_id,
        )


def upload_to_s3(file: AECBFile, bucket: str, prefix: str) -> str:
    """
    Archive the raw file to S3 before we do anything else.
    If the DB insert fails later, we still have the file and can replay.
    KMS encryption is required — unencrypted uploads will be rejected by bucket policy.
    """
    key = f"{prefix}/{file.received_at.strftime('%Y/%m/%d')}/{file.filename}"
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=file.content,
        ServerSideEncryption="aws:kms",
        Metadata={"checksum-sha256": file.checksum},
    )
    return f"s3://{bucket}/{key}"


def insert_bronze_records(records: list[AECBRecord], conn) -> int:
    """
    Bulk insert all records from one file in a single round-trip.
    ON CONFLICT DO NOTHING handles reruns gracefully — same checksum = same data, safe to skip.
    """
    rows = [
        (r.emirates_id, r.raw_xml, r.filename, r.file_received_at, r.batch_id, r.checksum_sha256)
        for r in records
    ]
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO bronze.aecb_raw
                (emirates_id, raw_xml, file_name, file_received_at, batch_id, checksum_sha256)
            VALUES %s
            ON CONFLICT DO NOTHING
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def run_aecb_ingestion(sftp_config: dict, db_dsn: str, s3_bucket: str, s3_prefix: str = "bronze/aecb") -> dict:
    """
    Main entry point — called by Airflow task.
    Processes all new files in one batch. Each file gets its own try/except so
    one bad file doesn't block the others.
    """
    batch_id = str(uuid.uuid4())
    total_files = 0
    total_records = 0
    errors = []

    with AECBSFTPClient(**sftp_config) as sftp:
        new_files = sftp.list_new_files()
        logger.info("AECB batch %s — %d new files to process", batch_id, len(new_files))

        with psycopg2.connect(db_dsn) as conn:
            for filename in new_files:
                try:
                    aecb_file = sftp.download_file(filename)
                    upload_to_s3(aecb_file, s3_bucket, s3_prefix)   # archive first
                    records = list(parse_aecb_xml(aecb_file, batch_id))
                    inserted = insert_bronze_records(records, conn)
                    sftp.mark_processed(filename)                    # only mark done after DB insert
                    total_files += 1
                    total_records += inserted
                    logger.info("%s: %d records inserted", filename, inserted)
                except Exception as exc:
                    errors.append(f"{filename}: {exc}")
                    logger.exception("Failed processing %s", filename)

    result = {
        "batch_id": batch_id,
        "files_processed": total_files,
        "records_inserted": total_records,
        "errors": errors,
    }
    logger.info("AECB ingestion done: %s", result)
    return result
