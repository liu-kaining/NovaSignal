"""Cloudflare R2 storage client with retry logic."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)

# Canonical report object layout (used for dedup, Hugo sync, and guards against stray keys).
REPORT_STORAGE_KEY_RE = re.compile(
    r"^reports/(?P<date>\d{4}-\d{2}-\d{2})/(?P<symbol>[^/]+)_report\.md$"
)


def parse_report_storage_key(key: str) -> tuple[str, str] | None:
    """If ``key`` matches ``reports/{date}/{SYMBOL}_report.md``, return (date, symbol)."""
    m = REPORT_STORAGE_KEY_RE.match(key)
    if not m:
        return None
    return m.group("date"), m.group("symbol")


class R2StorageError(RuntimeError):
    """Raised when R2 operations fail after retries."""


class R2Client:
    """Retry-aware client for Cloudflare R2 object storage.

    **Writes:** All ``upload_*`` helpers build a deterministic ``Key`` and call
    ``put_object``. R2/S3 semantics are *overwrite per key*: uploading again
    replaces the object body and does **not** append or create a second object.

    **Reads:** ``download_file`` uses ``get_object``; ``list_objects`` uses
    paginated ``list_objects_v2`` on a prefix.

    Bucket growth over time comes from **new keys** (e.g. new calendar days in
    the path, new symbols), not from retries of the same logical upload.
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        bucket_name: str | None = None,
        retry_attempts: int = 3,
        retry_min_wait_seconds: float = 1,
        retry_max_wait_seconds: float = 8,
        client: Any | None = None,
    ) -> None:
        self.bucket_name = bucket_name or os.getenv("R2_BUCKET_NAME", "novasignal")
        self._endpoint_url = endpoint_url or os.getenv("R2_ENDPOINT_URL")
        self._access_key_id = access_key_id or os.getenv("R2_ACCESS_KEY_ID")
        self._secret_access_key = secret_access_key or os.getenv("R2_SECRET_ACCESS_KEY")

        if client is not None:
            self._client = client
        else:
            if not self._endpoint_url:
                raise ValueError("R2_ENDPOINT_URL is required")
            if not self._access_key_id:
                raise ValueError("R2_ACCESS_KEY_ID is required")
            if not self._secret_access_key:
                raise ValueError("R2_SECRET_ACCESS_KEY is required")
            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                region_name="auto",
            )

        self._retryer = Retrying(
            stop=stop_after_attempt(retry_attempts),
            wait=wait_exponential(
                min=retry_min_wait_seconds,
                max=retry_max_wait_seconds,
            ),
            retry=retry_if_exception(_is_retryable_error),
            reraise=True,
            before_sleep=_log_retry,
        )

    def upload_report(self, symbol: str, content: str, report_date: str | date | datetime | None = None) -> str:
        """Upload a markdown report for a symbol. Returns the object key."""
        date_str = _resolve_date(report_date)
        key = f"reports/{date_str}/{symbol.upper()}_report.md"
        self._put_object(key, content.encode("utf-8"), content_type="text/markdown")
        return key

    def upload_metrics(self, symbol: str, metrics: dict[str, Any], report_date: str | date | datetime | None = None) -> str:
        """Upload metrics JSON for a symbol. Returns the object key."""
        date_str = _resolve_date(report_date)
        key = f"metrics/{date_str}/{symbol.upper()}_metrics.json"
        body = json.dumps(metrics, indent=2, ensure_ascii=False).encode("utf-8")
        self._put_object(key, body, content_type="application/json")
        return key

    def upload_raw_data(
        self,
        symbol: str,
        raw_data: dict[str, Any],
        report_date: str | date | datetime | None = None,
    ) -> str:
        """Upload FMP-enriched ``raw_data`` snapshot for a symbol (audit / replay)."""
        date_str = _resolve_date(report_date)
        key = f"raw_data/{date_str}/{symbol.upper()}_raw_data.json"
        body = json.dumps(
            raw_data,
            indent=2,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        self._put_object(key, body, content_type="application/json")
        return key

    def upload_fmp_prefetch_bundle(
        self,
        bundle: dict[str, Any],
        report_date: str | date | datetime | None = None,
    ) -> str:
        """Upload once-per-run FMP prefetches (IPO lists + global macro/sector snapshots).

        Key is stable per calendar day so repeated runs overwrite the same object;
        per-symbol ``raw_data`` retains a merged copy for reproducibility.
        """
        date_str = _resolve_date(report_date)
        key = f"raw_data/_shared/{date_str}/fmp_prefetch_bundle.json"
        body = json.dumps(
            bundle,
            indent=2,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        self._put_object(key, body, content_type="application/json")
        return key

    def upload_state_log(self, symbol: str, state: dict[str, Any], report_date: str | date | datetime | None = None) -> str:
        """Upload state JSON for a symbol. Returns the object key."""
        date_str = _resolve_date(report_date)
        key = f"state/{date_str}/{symbol.upper()}_state.json"
        body = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
        self._put_object(key, body, content_type="application/json")
        return key

    def download_file(self, key: str) -> bytes:
        """Download a file from R2 by key. Returns raw bytes."""
        LOGGER.info("Downloading %s from bucket %s", key, self.bucket_name)
        for attempt in self._retryer:
            with attempt:
                try:
                    response = self._client.get_object(
                        Bucket=self.bucket_name,
                        Key=key,
                    )
                    return response["Body"].read()
                except ClientError as exc:
                    error_code = exc.response.get("Error", {}).get("Code", "")
                    if error_code == "NoSuchKey":
                        raise R2StorageError(f"Object not found: {key}") from exc
                    raise
        raise R2StorageError(f"Failed to download {key} after retries")

    def list_objects(self, prefix: str) -> list[str]:
        """List object keys under a prefix."""
        LOGGER.info("Listing objects with prefix %s in bucket %s", prefix, self.bucket_name)
        keys: list[str] = []
        for attempt in self._retryer:
            with attempt:
                paginator = self._client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        keys.append(obj["Key"])
                return keys
        raise R2StorageError(f"Failed to list objects with prefix {prefix}")

    def upload_raw(self, key: str, body: bytes, *, content_type: str = "application/octet-stream") -> str:
        """Upload raw bytes to an arbitrary key. Returns the object key."""
        self._put_object(key, body, content_type=content_type)
        return key

    def _put_object(self, key: str, body: bytes, *, content_type: str) -> None:
        """Upload bytes to R2 with retry (creates or overwrites ``key``)."""
        LOGGER.info("Uploading %s to bucket %s", key, self.bucket_name)
        for attempt in self._retryer:
            with attempt:
                try:
                    self._client.put_object(
                        Bucket=self.bucket_name,
                        Key=key,
                        Body=body,
                        ContentType=content_type,
                    )
                except ClientError as exc:
                    error_code = exc.response.get("Error", {}).get("Code", "")
                    if error_code in ("InternalError", "ServiceUnavailable", "SlowDown", "RequestTimeout"):
                        LOGGER.warning("Retryable R2 error on upload %s: %s", key, error_code)
                        raise
                    raise R2StorageError(
                        f"Failed to upload {key}: {exc}"
                    ) from exc
        LOGGER.debug("Uploaded %s (%d bytes)", key, len(body))


def _resolve_date(value: str | date | datetime | None) -> str:
    """Resolve a date value to ISO format string, defaulting to today."""
    if value is None:
        return date.today().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    raise ValueError("date must be a non-empty string, date, or datetime")


def _is_retryable_error(exc: BaseException) -> bool:
    """Determine if a boto3 error is retryable."""
    if isinstance(exc, ClientError):
        error_code = exc.response.get("Error", {}).get("Code", "")
        return error_code in ("InternalError", "ServiceUnavailable", "SlowDown", "RequestTimeout")
    if isinstance(exc, R2StorageError):
        return False
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _log_retry(retry_state: Any) -> None:
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    LOGGER.warning(
        "Retrying R2 operation after attempt %s failed: %s",
        retry_state.attempt_number,
        exception,
    )
