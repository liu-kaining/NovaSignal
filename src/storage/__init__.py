"""Object storage adapters."""

from .r2_client import R2Client, R2StorageError

__all__ = ["R2Client", "R2StorageError"]
