import os
import unittest
from datetime import date, datetime
from unittest.mock import patch

import requests

from src.fetchers.fmp_client import FMPAPIError, FMPClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

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
            base_url="https://example.test/api/v3/",
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
                "url": "https://example.test/api/v3/ipo_calendar",
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


if __name__ == "__main__":
    unittest.main()
