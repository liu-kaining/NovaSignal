"""Financial Modeling Prep API client."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any

import requests
from tenacity import Retrying, retry_if_exception, stop_after_attempt

LOGGER = logging.getLogger(__name__)


class FMPAPIError(RuntimeError):
    """Raised when FMP returns an unusable response."""


class FMPClient:
    """Small, retry-aware client for Financial Modeling Prep data."""

    DEFAULT_BASE_URL = "https://financialmodelingprep.com/stable"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30,
        retry_attempts: int = 6,
        retry_min_wait_seconds: float = 5,
        retry_max_wait_seconds: float = 120,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("FMP_API_KEY")
        if not self.api_key:
            raise ValueError("FMP_API_KEY is required to initialize FMPClient")

        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._retry_min_wait_seconds = retry_min_wait_seconds
        self._retry_max_wait_seconds = retry_max_wait_seconds
        self.session = session or requests.Session()
        self._retryer = Retrying(
            stop=stop_after_attempt(retry_attempts),
            wait=self._wait_between_retries,
            retry=retry_if_exception(_is_retryable_exception),
            reraise=True,
            before_sleep=_log_retry,
        )

    @classmethod
    def from_config(cls, session: requests.Session | None = None) -> FMPClient:
        """Build a client using ``config/settings.yaml`` ``fmp`` section (API key still from env)."""
        from config.loader import get_fmp_settings

        fmp_cfg = get_fmp_settings()
        retry = fmp_cfg.get("retry") or {}
        return cls(
            base_url=fmp_cfg.get("base_url") or cls.DEFAULT_BASE_URL,
            timeout_seconds=float(fmp_cfg.get("timeout_seconds", 30)),
            retry_attempts=int(retry.get("attempts", 6)),
            retry_min_wait_seconds=float(retry.get("min_wait_seconds", 5)),
            retry_max_wait_seconds=float(retry.get("max_wait_seconds", 120)),
            session=session,
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
        data = self._get("/ipos-calendar", params=params)
        if not isinstance(data, list):
            raise FMPAPIError(
                f"Expected IPO calendar response to be a list, got {type(data).__name__}"
            )

        LOGGER.info("Fetched %s IPO calendar entries", len(data))
        return data

    def get_fundraising(self, cik: str) -> Any:
        """Fetch fundraising activity for a company by SEC Central Index Key (CIK)."""
        trimmed = cik.strip()
        if not trimmed:
            raise ValueError("cik must be a non-empty string")

        LOGGER.info("Fetching FMP fundraising data for CIK %s", trimmed)
        return self._get("/fundraising", params={"cik": trimmed})

    def get_stock_price(self, symbol: str) -> dict[str, Any]:
        """Fetch the current stock price quote for a symbol."""
        trimmed = symbol.strip().upper()
        if not trimmed:
            raise ValueError("symbol must be a non-empty string")

        LOGGER.info("Fetching stock price for %s", trimmed)
        data = self._get("/profile", params={"symbol": trimmed})
        if isinstance(data, list):
            if not data:
                raise FMPAPIError(f"No price data found for symbol {trimmed}")
            return data[0]
        if isinstance(data, dict):
            return data
        raise FMPAPIError(
            f"Unexpected response type for stock price: {type(data).__name__}"
        )

    def get_stock_price_historical(
        self,
        symbol: str,
        from_date: str | date | datetime,
        to_date: str | date | datetime,
    ) -> list[dict[str, Any]]:
        """Fetch historical daily price data for a symbol within a date range."""
        trimmed = symbol.strip().upper()
        if not trimmed:
            raise ValueError("symbol must be a non-empty string")

        params = {
            "symbol": trimmed,
            "from": _format_date(from_date),
            "to": _format_date(to_date),
        }
        LOGGER.info(
            "Fetching historical prices for %s from %s to %s",
            trimmed,
            params["from"],
            params["to"],
        )
        data = self._get("/historical-price-eod/full", params=params)
        return _normalize_historical_rows(data)

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

    def _wait_between_retries(self, retry_state: Any) -> float:
        """Honor Retry-After when present; otherwise exponential backoff in seconds."""
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            ra = exc.response.headers.get("Retry-After")
            if ra is not None:
                try:
                    wait_s = float(ra)
                    LOGGER.info("Sleeping %.1fs per FMP Retry-After header", wait_s)
                    return wait_s
                except ValueError:
                    pass

        if (
            self._retry_min_wait_seconds == 0
            and self._retry_max_wait_seconds == 0
        ):
            return 0.0

        n = retry_state.attempt_number
        wait = self._retry_min_wait_seconds * (2 ** (n - 1))
        return float(
            min(
                max(wait, self._retry_min_wait_seconds),
                self._retry_max_wait_seconds,
            )
        )


def _normalize_historical_rows(payload: Any) -> list[dict[str, Any]]:
    """Accept both a bare list and the stable API object with a ``historical`` array."""
    if isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
        if len(rows) != len(payload):
            raise FMPAPIError("Historical price list contained non-object entries")
        return rows
    if isinstance(payload, dict):
        hist = payload.get("historical")
        if isinstance(hist, list):
            rows = [x for x in hist if isinstance(x, dict)]
            if len(rows) != len(hist):
                raise FMPAPIError("Historical price historical[] contained non-object entries")
            return rows
    raise FMPAPIError(
        f"Unexpected historical price payload shape: {type(payload).__name__}"
    )


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
