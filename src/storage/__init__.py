"""Object storage adapters."""

from .r2_client import R2Client, R2StorageError, parse_report_storage_key

__all__ = ["R2Client", "R2StorageError", "parse_report_storage_key"]
