"""Financial Modeling Prep API client."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any

import requests
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)


class FMPAPIError(RuntimeError):
    """Raised when FMP returns an unusable response."""


class FMPClient:
    """Small, retry-aware client for Financial Modeling Prep data."""

    DEFAULT_BASE_URL = "https://financialmodelingprep.com/api/v3"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30,
        retry_attempts: int = 3,
        retry_min_wait_seconds: float = 2,
        retry_max_wait_seconds: float = 10,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("FMP_API_KEY")
        if not self.api_key:
            raise ValueError("FMP_API_KEY is required to initialize FMPClient")

        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self._retryer = Retrying(
            stop=stop_after_attempt(retry_attempts),
            wait=wait_exponential(
                min=retry_min_wait_seconds,
                max=retry_max_wait_seconds,
            ),
            retry=retry_if_exception(_is_retryable_exception),
            reraise=True,
            before_sleep=_log_retry,
        )

    def get_ipo_calendar(
        self,
        from_date: str | date | datetime,
        to_date: str | date | datetime,
    ) -> list[dict[str, Any]]:
        """Fetch IPO calendar entries for the inclusive date range."""
        params = {
            "from": _format_date(from_date),
            "to": _format_date(to_date),
        }

        LOGGER.info(
            "Fetching FMP IPO calendar from %s to %s",
            params["from"],
            params["to"],
        )
        data = self._get("/ipo_calendar", params=params)
        if not isinstance(data, list):
            raise FMPAPIError(
                f"Expected IPO calendar response to be a list, got {type(data).__name__}"
            )

        LOGGER.info("Fetched %s IPO calendar entries", len(data))
        return data

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        request_params = dict(params or {})
        request_params["apikey"] = self.api_key
        url = f"{self.base_url}/{path.lstrip('/')}"

        for attempt in self._retryer:
            with attempt:
                LOGGER.debug("GET %s with params=%s", url, _redact_api_key(request_params))
                response = self.session.get(
                    url,
                    params=request_params,
                    timeout=self.timeout_seconds,
                )
                _raise_for_status(response)
                return _parse_json(response)

        raise FMPAPIError("FMP request failed without returning a response")


def configure_logging(level: int = logging.INFO) -> None:
    """Configure standard logging for standalone scripts."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _format_date(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    raise ValueError("date values must be non-empty strings, date, or datetime objects")


def _parse_json(response: requests.Response) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        raise FMPAPIError("FMP response was not valid JSON") from exc

    if isinstance(payload, dict):
        error_message = payload.get("Error Message") or payload.get("error")
        if error_message:
            raise FMPAPIError(f"FMP returned an error: {error_message}")

    return payload


def _raise_for_status(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        status_code = response.status_code
        message = f"FMP HTTP error {status_code}"
        if status_code == 429 or status_code >= 500:
            LOGGER.warning("%s; request will be retried if attempts remain", message)
            raise
        raise FMPAPIError(message) from exc


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        if response is None:
            return True
        return response.status_code == 429 or response.status_code >= 500
    return False


def _log_retry(retry_state: Any) -> None:
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    LOGGER.warning(
        "Retrying FMP request after attempt %s failed: %s",
        retry_state.attempt_number,
        exception,
    )


def _redact_api_key(params: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(params)
    if "apikey" in redacted:
        redacted["apikey"] = "***"
    return redacted
