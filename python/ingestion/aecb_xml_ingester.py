"""
AECB (UAE Credit Bureau) XML Ingester
Source: batch SFTP delivery, XML format, matched on Emirates ID
Lands records in bronze.aecb_raw, signals silver ETL on completion.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

import boto3
import paramiko
import psycopg2
from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)


@dataclass
class AECBFile:
    filename: str
    content: bytes
    received_at: datetime
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
    def __init__(self, host: str, port: int, username: str, private_key_path: str, remote_dir: str):
        self.host = host
        self.port = port
        self.username = username
        self.private_key_path = private_key_path
        self.remote_dir = remote_dir
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    def __enter__(self):
        key = paramiko.RSAKey.from_private_key_file(self.private_key_path)
        self._client = paramiko.SSHClient()
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
        all_files = [f.filename for f in self._sftp.listdir_attr(self.remote_dir)
                     if f.filename.endswith(".xml")]
        processed = {f.filename.removeprefix(marker_prefix)
                     for f in self._sftp.listdir_attr(self.remote_dir)
                     if f.filename.startswith(marker_prefix)}
        return [f for f in all_files if f not in processed]

    def download_file(self, filename: str) -> AECBFile:
        remote_path = f"{self.remote_dir}/{filename}"
        with self._sftp.open(remote_path, "rb") as f:
            content = f.read()
        stat = self._sftp.stat(remote_path)
        received_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        return AECBFile(filename=filename, content=content, received_at=received_at)

    def mark_processed(self, filename: str):
        marker = f"{self.remote_dir}/processed_{filename}"
        self._sftp.open(marker, "w").close()


def parse_aecb_xml(file: AECBFile, batch_id: str) -> Iterator[AECBRecord]:
    """
    Parse AECB XML. Each <CreditReport> element maps to one customer.
    Yields one AECBRecord per customer in the file.
    """
    root = ET.fromstring(file.content.decode("utf-8"))
    ns = {"aecb": "http://www.aecb.gov.ae/schema/v2"}

    for report in root.findall(".//aecb:CreditReport", ns):
        emirates_id_el = report.find("aecb:EmiratesID", ns)
        if emirates_id_el is None or not emirates_id_el.text:
            logger.warning("CreditReport missing EmiratesID in %s — skipping", file.filename)
            continue

        # Serialise the individual <CreditReport> subtree for bronze storage
        report_xml = ET.tostring(report, encoding="unicode")

        yield AECBRecord(
            emirates_id=emirates_id_el.text.strip(),
            raw_xml=report_xml,
            filename=file.filename,
            file_received_at=file.received_at,
            checksum_sha256=file.checksum,
            batch_id=batch_id,
        )


def upload_to_s3(file: AECBFile, bucket: str, prefix: str) -> str:
    """Archive raw XML file to S3 for long-term retention."""
    key = f"{prefix}/{file.received_at.strftime('%Y/%m/%d')}/{file.filename}"
    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=file.content,
        ServerSideEncryption="aws:kms",
        Metadata={"checksum-sha256": file.checksum},
    )
    return f"s3://{bucket}/{key}"


def insert_bronze_records(records: list[AECBRecord], conn) -> int:
    rows = [
        (
            r.emirates_id,
            r.raw_xml,
            r.filename,
            r.file_received_at,
            r.batch_id,
            r.checksum_sha256,
        )
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


def run_aecb_ingestion(
    sftp_config: dict,
    db_dsn: str,
    s3_bucket: str,
    s3_prefix: str = "bronze/aecb",
) -> dict:
    batch_id = str(uuid.uuid4())
    total_files = 0
    total_records = 0
    errors: list[str] = []

    with AECBSFTPClient(**sftp_config) as sftp:
        new_files = sftp.list_new_files()
        logger.info("AECB batch %s: %d new files found", batch_id, len(new_files))

        with psycopg2.connect(db_dsn) as conn:
            for filename in new_files:
                try:
                    aecb_file = sftp.download_file(filename)
                    upload_to_s3(aecb_file, s3_bucket, s3_prefix)
                    records = list(parse_aecb_xml(aecb_file, batch_id))
                    inserted = insert_bronze_records(records, conn)
                    sftp.mark_processed(filename)
                    total_files += 1
                    total_records += inserted
                    logger.info("Processed %s: %d records", filename, inserted)
                except Exception as exc:
                    errors.append(f"{filename}: {exc}")
                    logger.exception("Failed to process %s", filename)

    summary = {
        "batch_id": batch_id,
        "files_processed": total_files,
        "records_inserted": total_records,
        "errors": errors,
    }
    logger.info("AECB ingestion complete: %s", summary)
    return summary
