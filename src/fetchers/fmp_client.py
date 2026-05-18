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

    def get_company_profile(self, symbol: str) -> dict[str, Any]:
        """Alias for :meth:`get_stock_price` — FMP ``/profile`` includes fundamentals + identifiers."""
        return self.get_stock_price(symbol)

    def get_company_notes(self, symbol: str) -> list[dict[str, Any]]:
        """Company descriptive notes / footnotes (``/company-notes``)."""
        trimmed = _require_symbol(symbol)
        data = self._get("/company-notes", params={"symbol": trimmed})
        return _normalize_list_of_dicts(data, "company-notes")

    def get_income_statement(
        self,
        symbol: str,
        *,
        period: str = "annual",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Income statements (annual or quarter)."""
        return self._get_symbol_period_table(
            "/income-statement", symbol, period=period, limit=limit
        )

    def get_balance_sheet_statement(
        self,
        symbol: str,
        *,
        period: str = "annual",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Balance sheet statements (annual or quarter)."""
        return self._get_symbol_period_table(
            "/balance-sheet-statement", symbol, period=period, limit=limit
        )

    def get_cash_flow_statement(
        self,
        symbol: str,
        *,
        period: str = "annual",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Cash flow statements (annual or quarter)."""
        return self._get_symbol_period_table(
            "/cash-flow-statement", symbol, period=period, limit=limit
        )

    def get_key_metrics(
        self,
        symbol: str,
        *,
        period: str = "annual",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Key metrics time series (annual or quarter)."""
        return self._get_symbol_period_table(
            "/key-metrics", symbol, period=period, limit=limit
        )

    def get_key_metrics_ttm(self, symbol: str) -> Any:
        """Trailing twelve months key metrics (shape may be list or dict depending on FMP)."""
        trimmed = _require_symbol(symbol)
        LOGGER.info("Fetching key-metrics-ttm for %s", trimmed)
        return self._get("/key-metrics-ttm", params={"symbol": trimmed})

    def get_ratios(
        self,
        symbol: str,
        *,
        period: str = "annual",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Financial ratios (annual or quarter)."""
        return self._get_symbol_period_table("/ratios", symbol, period=period, limit=limit)

    def get_ratios_ttm(self, symbol: str) -> Any:
        """Trailing twelve months ratios (shape may be list or dict depending on FMP)."""
        trimmed = _require_symbol(symbol)
        LOGGER.info("Fetching ratios-ttm for %s", trimmed)
        return self._get("/ratios-ttm", params={"symbol": trimmed})

    def get_enterprise_values(
        self,
        symbol: str,
        *,
        period: str = "annual",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Enterprise value history (annual or quarter)."""
        return self._get_symbol_period_table(
            "/enterprise-values", symbol, period=period, limit=limit
        )

    def get_stock_news(self, symbol: str, *, limit: int = 30) -> list[dict[str, Any]]:
        """Symbol-filtered stock news headlines (``/news/stock``)."""
        trimmed = _require_symbol(symbol)
        LOGGER.info("Fetching stock news for %s (limit=%s)", trimmed, limit)
        params: dict[str, Any] = {"symbols": trimmed, "limit": limit}
        data = self._get("/news/stock", params=params)
        return _normalize_list_of_dicts(data, "stock news")

    def get_press_releases(self, symbol: str, *, limit: int = 25) -> list[dict[str, Any]]:
        """Company press releases for a symbol (``/news/press-releases``)."""
        trimmed = _require_symbol(symbol)
        LOGGER.info("Fetching press releases for %s (limit=%s)", trimmed, limit)
        params: dict[str, Any] = {"symbols": trimmed, "limit": limit}
        data = self._get("/news/press-releases", params=params)
        return _normalize_list_of_dicts(data, "press releases")

    def get_ipos_disclosure(self) -> list[dict[str, Any]]:
        """Global IPO regulatory disclosure listing (filter client-side by symbol / CIK)."""
        LOGGER.info("Fetching FMP ipos-disclosure (full list)")
        data = self._get("/ipos-disclosure", params={})
        return _normalize_list_of_dicts(data, "ipos-disclosure")

    def get_ipos_prospectus(self) -> list[dict[str, Any]]:
        """Global IPO prospectus listing (filter client-side by symbol / CIK)."""
        LOGGER.info("Fetching FMP ipos-prospectus (full list)")
        data = self._get("/ipos-prospectus", params={})
        return _normalize_list_of_dicts(data, "ipos-prospectus")

    def get_stock_peers(self, symbol: str) -> list[dict[str, Any]]:
        """Peer / comparable companies (``/stock-peers``)."""
        trimmed = _require_symbol(symbol)
        data = self._get("/stock-peers", params={"symbol": trimmed})
        return _normalize_list_of_dicts(data, "stock-peers")

    def get_key_executives(self, symbol: str) -> list[dict[str, Any]]:
        """Key executives (``/key-executives``)."""
        trimmed = _require_symbol(symbol)
        data = self._get("/key-executives", params={"symbol": trimmed})
        return _normalize_list_of_dicts(data, "key-executives")

    def get_shares_float(self, symbol: str) -> Any:
        """Shares float snapshot (``/shares-float``)."""
        trimmed = _require_symbol(symbol)
        return self._get("/shares-float", params={"symbol": trimmed})

    def get_quote(self, symbol: str) -> Any:
        """Real-time style quote (``/quote``; fields depend on FMP / market hours)."""
        trimmed = _require_symbol(symbol)
        return self._get("/quote", params={"symbol": trimmed})

    def get_financial_scores(self, symbol: str) -> Any:
        """Financial health scores e.g. Altman Z, Piotroski (``/financial-scores``)."""
        trimmed = _require_symbol(symbol)
        return self._get("/financial-scores", params={"symbol": trimmed})

    def get_analyst_estimates(
        self,
        symbol: str,
        *,
        period: str = "annual",
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Analyst financial estimates (``/analyst-estimates``)."""
        trimmed = _require_symbol(symbol)
        params: dict[str, Any] = {
            "symbol": trimmed,
            "period": period,
            "limit": int(limit),
        }
        data = self._get("/analyst-estimates", params=params)
        return _normalize_list_of_dicts(data, "analyst-estimates")

    def get_price_target_summary(self, symbol: str) -> Any:
        """Analyst price target summary (``/price-target-summary``)."""
        trimmed = _require_symbol(symbol)
        return self._get("/price-target-summary", params={"symbol": trimmed})

    def get_price_target_consensus(self, symbol: str) -> Any:
        """Price target consensus high/low/median (``/price-target-consensus``)."""
        trimmed = _require_symbol(symbol)
        return self._get("/price-target-consensus", params={"symbol": trimmed})

    def get_ratings_snapshot(self, symbol: str) -> Any:
        """Analyst ratings snapshot (``/ratings-snapshot``)."""
        trimmed = _require_symbol(symbol)
        return self._get("/ratings-snapshot", params={"symbol": trimmed})

    def get_sec_filings_symbol(
        self,
        symbol: str,
        from_date: str | date | datetime,
        to_date: str | date | datetime,
        *,
        page: int = 0,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """SEC filings for symbol in date range (``/sec-filings-search/symbol``)."""
        trimmed = _require_symbol(symbol)
        params: dict[str, Any] = {
            "symbol": trimmed,
            "from": _format_date(from_date),
            "to": _format_date(to_date),
            "page": int(page),
            "limit": int(limit),
        }
        data = self._get("/sec-filings-search/symbol", params=params)
        return _normalize_list_of_dicts(data, "sec-filings-search/symbol")

    def get_insider_trading_statistics(self, symbol: str) -> Any:
        """Insider trading aggregates for symbol (``/insider-trading/statistics``)."""
        trimmed = _require_symbol(symbol)
        return self._get("/insider-trading/statistics", params={"symbol": trimmed})

    def get_insider_trading_search(
        self,
        symbol: str,
        *,
        page: int = 0,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Insider trades search (``/insider-trading/search``; symbol filter if supported)."""
        trimmed = _require_symbol(symbol)
        params: dict[str, Any] = {
            "symbol": trimmed,
            "page": int(page),
            "limit": int(limit),
        }
        data = self._get("/insider-trading/search", params=params)
        return _normalize_list_of_dicts(data, "insider-trading/search")

    def get_revenue_product_segmentation(self, symbol: str) -> list[dict[str, Any]]:
        """Revenue by product line (``/revenue-product-segmentation``)."""
        trimmed = _require_symbol(symbol)
        data = self._get("/revenue-product-segmentation", params={"symbol": trimmed})
        return _normalize_list_of_dicts(data, "revenue-product-segmentation")

    def get_revenue_geographic_segmentation(self, symbol: str) -> list[dict[str, Any]]:
        """Revenue by geography (``/revenue-geographic-segmentation``)."""
        trimmed = _require_symbol(symbol)
        data = self._get("/revenue-geographic-segmentation", params={"symbol": trimmed})
        return _normalize_list_of_dicts(data, "revenue-geographic-segmentation")

    def get_treasury_rates(self) -> list[dict[str, Any]]:
        """Treasury yield curve time series (``/treasury-rates``)."""
        LOGGER.info("Fetching treasury rates")
        data = self._get("/treasury-rates", params={})
        return _normalize_list_of_dicts(data, "treasury-rates")

    def get_market_risk_premium(self) -> Any:
        """Equity market risk premium (``/market-risk-premium``)."""
        LOGGER.info("Fetching market risk premium")
        return self._get("/market-risk-premium", params={})

    def get_economic_calendar(
        self,
        from_date: str | date | datetime,
        to_date: str | date | datetime,
    ) -> list[dict[str, Any]]:
        """Scheduled macro data releases (``/economic-calendar``)."""
        params = {
            "from": _format_date(from_date),
            "to": _format_date(to_date),
        }
        LOGGER.info(
            "Fetching economic calendar %s .. %s", params["from"], params["to"]
        )
        data = self._get("/economic-calendar", params=params)
        return _normalize_list_of_dicts(data, "economic-calendar")

    def get_economic_indicators(self, name: str) -> list[dict[str, Any]]:
        """Macro indicator series (``/economic-indicators``); ``name`` e.g. GDP, unemploymentRate."""
        label = name.strip()
        if not label:
            raise ValueError("economic indicator name must be non-empty")
        LOGGER.info("Fetching economic indicators name=%s", label)
        data = self._get("/economic-indicators", params={"name": label})
        return _normalize_list_of_dicts(data, f"economic-indicators:{label}")

    def get_sector_performance_snapshot(
        self, as_of: str | date | datetime
    ) -> list[dict[str, Any]]:
        """ALL sectors — one-day performance snapshot (``/sector-performance-snapshot``)."""
        params = {"date": _format_date(as_of)}
        data = self._get("/sector-performance-snapshot", params=params)
        return _normalize_list_of_dicts(data, "sector-performance-snapshot")

    def get_industry_performance_snapshot(
        self, as_of: str | date | datetime
    ) -> list[dict[str, Any]]:
        """ALL industries — one-day snapshot (``/industry-performance-snapshot``)."""
        params = {"date": _format_date(as_of)}
        data = self._get("/industry-performance-snapshot", params=params)
        return _normalize_list_of_dicts(data, "industry-performance-snapshot")

    def get_sector_pe_snapshot(
        self, as_of: str | date | datetime
    ) -> list[dict[str, Any]]:
        """Sector valuation P/E snapshot (``/sector-pe-snapshot``)."""
        params = {"date": _format_date(as_of)}
        data = self._get("/sector-pe-snapshot", params=params)
        return _normalize_list_of_dicts(data, "sector-pe-snapshot")

    def get_industry_pe_snapshot(
        self, as_of: str | date | datetime
    ) -> list[dict[str, Any]]:
        """Industry P/E snapshot (``/industry-pe-snapshot``)."""
        params = {"date": _format_date(as_of)}
        data = self._get("/industry-pe-snapshot", params=params)
        return _normalize_list_of_dicts(data, "industry-pe-snapshot")

    def get_historical_sector_performance(self, sector: str) -> list[dict[str, Any]]:
        """Time series of sector performance for one sector (``/historical-sector-performance``)."""
        label = sector.strip()
        if not label:
            raise ValueError("sector must be a non-empty string")
        data = self._get("/historical-sector-performance", params={"sector": label})
        return _normalize_list_of_dicts(data, "historical-sector-performance")

    def get_historical_industry_performance(self, industry: str) -> list[dict[str, Any]]:
        """Time series of industry performance (``/historical-industry-performance``)."""
        label = industry.strip()
        if not label:
            raise ValueError("industry must be a non-empty string")
        data = self._get("/historical-industry-performance", params={"industry": label})
        return _normalize_list_of_dicts(data, "historical-industry-performance")

    def get_etf_sector_weightings(self, symbol: str = "SPY") -> list[dict[str, Any]]:
        """ETF / index basket sector breakdown (e.g. SPY) — ``/etf/sector-weightings``."""
        trimmed = _require_symbol(symbol)
        data = self._get("/etf/sector-weightings", params={"symbol": trimmed})
        return _normalize_list_of_dicts(data, "etf/sector-weightings")

    def get_batch_quote(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Multiple symbols in one call (``/batch-quote``)."""
        parts = [s.strip().upper() for s in symbols if s and str(s).strip()]
        if not parts:
            return []
        params = {"symbols": ",".join(parts)}
        data = self._get("/batch-quote", params=params)
        return _normalize_list_of_dicts(data, "batch-quote")

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

    def _get_symbol_period_table(
        self,
        path: str,
        symbol: str,
        *,
        period: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        trimmed = _require_symbol(symbol)
        if period not in ("annual", "quarter"):
            raise ValueError("period must be 'annual' or 'quarter'")
        LOGGER.info(
            "Fetching %s for %s period=%s limit=%s",
            path,
            trimmed,
            period,
            limit,
        )
        params: dict[str, Any] = {
            "symbol": trimmed,
            "period": period,
            "limit": int(limit),
        }
        data = self._get(path, params=params)
        return _normalize_list_of_dicts(data, path)

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


def _require_symbol(symbol: str) -> str:
    trimmed = symbol.strip().upper()
    if not trimmed:
        raise ValueError("symbol must be a non-empty string")
    return trimmed


def _normalize_list_of_dicts(payload: Any, label: str) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
        if len(rows) != len(payload):
            raise FMPAPIError(f"{label}: list contained non-object entries")
        return rows
    if isinstance(payload, dict):
        # Some endpoints may return a single row as object
        return [payload]
    raise FMPAPIError(
        f"{label}: unexpected payload type {type(payload).__name__}"
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
