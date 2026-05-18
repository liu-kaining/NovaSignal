import os
import unittest
from datetime import date, datetime
from unittest.mock import patch

import requests

from src.fetchers.fmp_client import FMPAPIError, FMPClient


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}",
                response=self,
            )


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append(
            {
                "url": url,
                "params": params,
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FMPClientTest(unittest.TestCase):
    def test_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                FMPClient()

    def test_get_ipo_calendar_uses_expected_endpoint_and_dates(self):
        session = FakeSession(
            [
                FakeResponse(
                    [
                        {
                            "symbol": "NOVA",
                            "date": "2026-05-09",
                        }
                    ]
                )
            ]
        )
        client = FMPClient(
            api_key="test-key",
            base_url="https://example.test/stable/",
            timeout_seconds=5,
            session=session,
        )

        result = client.get_ipo_calendar(
            datetime(2026, 5, 9, 12, 30),
            date(2026, 5, 10),
        )

        self.assertEqual(result, [{"symbol": "NOVA", "date": "2026-05-09"}])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            session.calls[0],
            {
                "url": "https://example.test/stable/ipos-calendar",
                "params": {
                    "from": "2026-05-09",
                    "to": "2026-05-10",
                    "apikey": "test-key",
                },
                "timeout": 5,
            },
        )

    def test_retries_rate_limit_and_then_succeeds(self):
        session = FakeSession(
            [
                FakeResponse({"error": "rate limited"}, status_code=429),
                FakeResponse([]),
            ]
        )
        client = FMPClient(
            api_key="test-key",
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
            session=session,
        )

        self.assertEqual(client.get_ipo_calendar("2026-05-09", "2026-05-10"), [])
        self.assertEqual(len(session.calls), 2)

    def test_non_retryable_client_error_raises_fmp_error(self):
        session = FakeSession([FakeResponse({"error": "bad key"}, status_code=401)])
        client = FMPClient(api_key="bad-key", session=session)

        with self.assertRaises(FMPAPIError):
            client.get_ipo_calendar("2026-05-09", "2026-05-10")
        self.assertEqual(len(session.calls), 1)

    def test_rejects_unexpected_payload_shape(self):
        session = FakeSession([FakeResponse({"unexpected": "shape"})])
        client = FMPClient(api_key="test-key", session=session)

        with self.assertRaises(FMPAPIError):
            client.get_ipo_calendar("2026-05-09", "2026-05-10")

    def test_get_fundraising_uses_stable_path_and_cik(self):
        session = FakeSession([FakeResponse([{"round": "series-a"}])])
        client = FMPClient(
            api_key="test-key",
            base_url="https://example.test/stable",
            session=session,
        )

        self.assertEqual(client.get_fundraising(" 0001547416 "), [{"round": "series-a"}])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            session.calls[0],
            {
                "url": "https://example.test/stable/fundraising",
                "params": {"cik": "0001547416", "apikey": "test-key"},
                "timeout": 30,
            },
        )

    def test_get_stock_price_historical_uses_stable_eod_full_endpoint(self):
        session = FakeSession([FakeResponse([
            {"date": "2026-01-15", "close": 100.0, "adjClose": 99.5}
        ])])
        client = FMPClient(
            api_key="test-key",
            base_url="https://example.test/stable",
            session=session,
        )

        result = client.get_stock_price_historical("AAPL", "2026-01-15", "2026-01-15")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["close"], 100.0)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(
            call["url"],
            "https://example.test/stable/historical-price-eod/full",
        )
        self.assertEqual(call["params"]["symbol"], "AAPL")
        self.assertEqual(call["params"]["from"], "2026-01-15")
        self.assertEqual(call["params"]["to"], "2026-01-15")

    def test_get_stock_price_historical_accepts_wrapped_payload(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "symbol": "AAPL",
                        "historical": [
                            {"date": "2026-01-15", "close": 42.0},
                        ],
                    }
                )
            ]
        )
        client = FMPClient(
            api_key="test-key",
            base_url="https://example.test/stable",
            session=session,
        )
        rows = client.get_stock_price_historical("AAPL", "2026-01-15", "2026-01-15")
        self.assertEqual(rows, [{"date": "2026-01-15", "close": 42.0}])

    @patch("config.loader.get_fmp_settings")
    def test_from_config_applies_yaml_retry_settings(self, mock_settings):
        mock_settings.return_value = {
            "base_url": "https://cfg.example/stable",
            "timeout_seconds": 99,
            "retry": {
                "attempts": 2,
                "min_wait_seconds": 1,
                "max_wait_seconds": 3,
            },
        }
        with patch.dict(os.environ, {"FMP_API_KEY": "k"}):
            client = FMPClient.from_config()
        self.assertEqual(client.base_url, "https://cfg.example/stable")
        self.assertEqual(client.timeout_seconds, 99)

    def test_get_income_statement_uses_period_and_limit(self):
        session = FakeSession([FakeResponse([{"revenue": 100}])])
        client = FMPClient(
            api_key="test-key",
            base_url="https://example.test/stable",
            session=session,
        )
        rows = client.get_income_statement("Nvda", period="quarter", limit=3)
        self.assertEqual(rows, [{"revenue": 100}])
        self.assertEqual(session.calls[0]["url"], "https://example.test/stable/income-statement")
        self.assertEqual(session.calls[0]["params"]["symbol"], "NVDA")
        self.assertEqual(session.calls[0]["params"]["period"], "quarter")
        self.assertEqual(session.calls[0]["params"]["limit"], 3)

    def test_get_stock_news_hits_news_stock_path(self):
        session = FakeSession([FakeResponse([{"title": "Hello"}])])
        client = FMPClient(
            api_key="test-key",
            base_url="https://example.test/stable",
            session=session,
        )
        news = client.get_stock_news("AAPL", limit=5)
        self.assertEqual(news, [{"title": "Hello"}])
        self.assertEqual(session.calls[0]["url"], "https://example.test/stable/news/stock")
        self.assertEqual(session.calls[0]["params"]["symbols"], "AAPL")
        self.assertEqual(session.calls[0]["params"]["limit"], 5)

    def test_get_ipos_disclosure_hits_stable_path(self):
        session = FakeSession([FakeResponse([{"cik": "1"}])])
        client = FMPClient(
            api_key="test-key",
            base_url="https://example.test/stable",
            session=session,
        )
        rows = client.get_ipos_disclosure()
        self.assertEqual(rows, [{"cik": "1"}])
        self.assertEqual(session.calls[0]["url"], "https://example.test/stable/ipos-disclosure")

    def test_get_batch_quote_joins_symbols(self):
        session = FakeSession([FakeResponse([{"symbol": "^GSPC"}])])
        client = FMPClient(
            api_key="test-key",
            base_url="https://example.test/stable",
            session=session,
        )
        rows = client.get_batch_quote(["^GSPC", "^VIX"])
        self.assertEqual(rows, [{"symbol": "^GSPC"}])
        self.assertEqual(
            session.calls[0]["url"], "https://example.test/stable/batch-quote"
        )
        self.assertEqual(session.calls[0]["params"]["symbols"], "^GSPC,^VIX")


if __name__ == "__main__":
    unittest.main()
