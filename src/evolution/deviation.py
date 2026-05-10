"""Deviation calculation between predictions and actual market outcomes."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from src.fetchers.fmp_client import FMPClient, configure_logging
from src.storage.r2_client import R2Client, R2StorageError

LOGGER = logging.getLogger(__name__)


class DeviationError(RuntimeError):
    """Raised when deviation calculation fails."""


class DeviationCalculator:
    """Evaluates prediction accuracy by comparing metrics against actual prices."""

    HISTORY_KEY = "state/evolution/deviation_history.json"

    def __init__(
        self,
        fmp: FMPClient,
        r2: R2Client,
        *,
        threshold: float = 0.15,
    ) -> None:
        self._fmp = fmp
        self._r2 = r2
        self._threshold = threshold

    def evaluate(self, symbol: str, metrics_date: str, *, lookback_offset_days: int = 30) -> dict[str, Any]:
        """Evaluate deviation for a single symbol on a given date.

        Compares the predicted price from stored metrics against the actual
        stock price T+30 days after the analysis date.
        """
        LOGGER.info("Evaluating deviation for %s on %s", symbol, metrics_date)

        metrics = self._load_metrics(symbol, metrics_date)
        predicted_price = metrics.get("predicted_price")
        if predicted_price is None:
            raise DeviationError(
                f"No predicted_price in metrics for {symbol} on {metrics_date}"
            )

        # T+30: compare against price 30 days after the analysis date
        analysis_date = date.fromisoformat(metrics_date)
        target_date = analysis_date + timedelta(days=lookback_offset_days)
        actual_price = self._fetch_actual_price(symbol, target_date.isoformat())
        deviation = self._calculate_deviation(predicted_price, actual_price)

        result = {
            "symbol": symbol,
            "date": metrics_date,
            "predicted_price": predicted_price,
            "actual_price": actual_price,
            "deviation": deviation,
            "deviation_pct": deviation * 100,
            "within_threshold": abs(deviation) <= self._threshold,
            "confidence": metrics.get("confidence"),
        }

        LOGGER.info(
            "Deviation for %s: %.2f%% (threshold: %.2f%%)",
            symbol,
            deviation * 100,
            self._threshold * 100,
        )
        return result

    def evaluate_batch(
        self,
        symbols: list[str],
        metrics_date: str,
    ) -> list[dict[str, Any]]:
        """Evaluate deviation for multiple symbols."""
        results = []
        for symbol in symbols:
            try:
                result = self.evaluate(symbol, metrics_date)
                results.append(result)
            except (DeviationError, R2StorageError) as exc:
                LOGGER.warning("Skipping %s: %s", symbol, exc)
                results.append({
                    "symbol": symbol,
                    "date": metrics_date,
                    "error": str(exc),
                    "within_threshold": None,
                })
        return results

    def save_history(self, results: list[dict[str, Any]]) -> str:
        """Append results to the deviation history in R2."""
        try:
            existing_bytes = self._r2.download_file(self.HISTORY_KEY)
            history = json.loads(existing_bytes.decode("utf-8"))
        except R2StorageError:
            history = []

        history.extend(results)

        body = json.dumps(history, indent=2, ensure_ascii=False).encode("utf-8")
        self._r2.upload_raw(
            self.HISTORY_KEY, body, content_type="application/json"
        )
        LOGGER.info("Saved %d entries to deviation history", len(results))
        return self.HISTORY_KEY

    def _load_metrics(self, symbol: str, metrics_date: str) -> dict[str, Any]:
        """Load metrics JSON from R2."""
        key = f"metrics/{metrics_date}/{symbol.upper()}_metrics.json"
        try:
            data = self._r2.download_file(key)
            return json.loads(data.decode("utf-8"))
        except R2StorageError as exc:
            raise DeviationError(f"Cannot load metrics for {symbol}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise DeviationError(f"Invalid metrics JSON for {symbol}: {exc}") from exc

    def _fetch_actual_price(self, symbol: str, target_date: str) -> float:
        """Fetch the actual closing price for a symbol near the target date.

        Searches within a 5-day window after target_date to handle weekends
        and holidays. Returns the first available trading day's close price.
        """
        try:
            start_d = date.fromisoformat(target_date)
            end_d = start_d + timedelta(days=5)
            data = self._fmp.get_stock_price_historical(
                symbol, start_d.isoformat(), end_d.isoformat()
            )
            if not data:
                raise DeviationError(
                    f"No historical price data for {symbol} between {target_date} and {end_d}"
                )

            def _row_date(row: dict[str, Any]) -> str:
                return str(row.get("date") or "")

            ordered = sorted(data, key=_row_date)
            chosen: dict[str, Any] | None = None
            for row in ordered:
                if _row_date(row) >= target_date:
                    chosen = row
                    break
            if chosen is None:
                chosen = ordered[-1]

            price = chosen.get("close") or chosen.get("adjClose")
            if price is None:
                raise DeviationError(
                    f"No close price in data for {symbol} on {target_date}"
                )
            return float(price)
        except Exception as exc:
            if isinstance(exc, DeviationError):
                raise
            raise DeviationError(
                f"Failed to fetch price for {symbol}: {exc}"
            ) from exc

    @staticmethod
    def _calculate_deviation(predicted: float, actual: float) -> float:
        """Calculate relative deviation between predicted and actual price."""
        if actual == 0:
            return 0.0
        return (predicted - actual) / actual


def main() -> None:
    """CLI entry point for deviation calculation."""
    parser = argparse.ArgumentParser(description="NovaSignal Deviation Calculator")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols to evaluate")
    parser.add_argument("--date", required=True, help="Metrics date (YYYY-MM-DD)")
    parser.add_argument("--threshold", type=float, default=0.15, help="Deviation threshold")
    parser.add_argument("--save", action="store_true", help="Save results to R2 history")

    args = parser.parse_args()
    configure_logging()

    try:
        fmp = FMPClient.from_config()
    except Exception:
        fmp = FMPClient()
    r2 = R2Client()
    calculator = DeviationCalculator(fmp, r2, threshold=args.threshold)

    results = calculator.evaluate_batch(args.symbols, args.date)

    for r in results:
        if r.get("error"):
            LOGGER.warning("%s: %s", r["symbol"], r["error"])
        else:
            status = "PASS" if r["within_threshold"] else "FAIL"
            LOGGER.info(
                "%s %s: deviation=%.2f%%",
                status,
                r["symbol"],
                r.get("deviation_pct", 0),
            )

    if args.save:
        calculator.save_history(results)

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
