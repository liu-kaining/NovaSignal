import json
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from src.storage.r2_client import R2Client, R2StorageError


class FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class R2ClientTest(unittest.TestCase):
    def _make_client(self, **kwargs):
        mock_s3 = MagicMock()
        return R2Client(
            bucket_name="test-bucket",
            client=mock_s3,
            retry_min_wait_seconds=0,
            retry_max_wait_seconds=0,
            **kwargs,
        ), mock_s3

    def test_requires_endpoint_url_without_client(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                R2Client(bucket_name="b")
            self.assertIn("R2_ENDPOINT_URL", str(ctx.exception))

    def test_upload_report_key_structure(self):
        client, mock_s3 = self._make_client()
        key = client.upload_report("AAPL", "# Report", report_date="2026-05-09")

        self.assertEqual(key, "reports/2026-05-09/AAPL_report.md")
        mock_s3.put_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="reports/2026-05-09/AAPL_report.md",
            Body=b"# Report",
            ContentType="text/markdown",
        )

    def test_upload_report_symbol_uppercased(self):
        client, mock_s3 = self._make_client()
        key = client.upload_report("aapl", "content", report_date="2026-01-01")
        self.assertEqual(key, "reports/2026-01-01/AAPL_report.md")

    def test_upload_metrics_key_and_content_type(self):
        client, mock_s3 = self._make_client()
        metrics = {"confidence": 0.85, "signal": "bullish"}
        key = client.upload_metrics("TSLA", metrics, report_date=date(2026, 3, 15))

        self.assertEqual(key, "metrics/2026-03-15/TSLA_metrics.json")
        call_kwargs = mock_s3.put_object.call_args[1]
        self.assertEqual(call_kwargs["ContentType"], "application/json")
        parsed = json.loads(call_kwargs["Body"])
        self.assertEqual(parsed["confidence"], 0.85)

    def test_upload_raw_data_key_and_default_str(self):
        client, mock_s3 = self._make_client()
        raw = {"symbol": "AAPL", "deep": {"d": date(2026, 1, 1)}}
        key = client.upload_raw_data("aapl", raw, report_date="2026-02-01")

        self.assertEqual(key, "raw_data/2026-02-01/AAPL_raw_data.json")
        call_kwargs = mock_s3.put_object.call_args[1]
        self.assertEqual(call_kwargs["ContentType"], "application/json")
        parsed = json.loads(call_kwargs["Body"])
        self.assertEqual(parsed["symbol"], "AAPL")
        self.assertEqual(parsed["deep"]["d"], "2026-01-01")

    def test_upload_fmp_prefetch_bundle_key(self):
        client, mock_s3 = self._make_client()
        bundle = {"ipo_regulatory_lists": {"disclosures": []}, "global_market_context": {}}
        key = client.upload_fmp_prefetch_bundle(bundle, report_date="2026-03-01")

        self.assertEqual(key, "raw_data/_shared/2026-03-01/fmp_prefetch_bundle.json")
        mock_s3.put_object.assert_called_once()

    def test_upload_state_log_key_structure(self):
        client, mock_s3 = self._make_client()
        state = {"status": "completed", "agent_version": "1.0"}
        key = client.upload_state_log("GOOG", state, report_date=datetime(2026, 6, 1, 10, 30))

        self.assertEqual(key, "state/2026-06-01/GOOG_state.json")

    def test_upload_report_defaults_to_today(self):
        client, mock_s3 = self._make_client()
        with patch("src.storage.r2_client.date") as mock_date:
            mock_date.today.return_value = date(2026, 7, 4)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            key = client.upload_report("MSFT", "report content")
        self.assertIn("2026-07-04", key)

    def test_download_file_returns_bytes(self):
        client, mock_s3 = self._make_client()
        mock_s3.get_object.return_value = {"Body": FakeBody(b"file content")}

        result = client.download_file("reports/2026-01-01/AAPL_report.md")
        self.assertEqual(result, b"file content")
        mock_s3.get_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="reports/2026-01-01/AAPL_report.md",
        )

    def test_download_file_not_found_raises_storage_error(self):
        client, mock_s3 = self._make_client()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )

        with self.assertRaises(R2StorageError) as ctx:
            client.download_file("missing/key.json")
        self.assertIn("not found", str(ctx.exception).lower())

    def test_list_objects_returns_keys(self):
        client, mock_s3 = self._make_client()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "reports/2026-01-01/A.md"}, {"Key": "reports/2026-01-01/B.md"}]},
            {"Contents": [{"Key": "reports/2026-01-02/C.md"}]},
        ]
        mock_s3.get_paginator.return_value = paginator

        keys = client.list_objects("reports/")
        self.assertEqual(keys, [
            "reports/2026-01-01/A.md",
            "reports/2026-01-01/B.md",
            "reports/2026-01-02/C.md",
        ])

    def test_list_objects_empty_prefix(self):
        client, mock_s3 = self._make_client()
        paginator = MagicMock()
        paginator.paginate.return_value = [{}]
        mock_s3.get_paginator.return_value = paginator

        keys = client.list_objects("nonexistent/")
        self.assertEqual(keys, [])

    def test_retry_on_internal_error(self):
        client, mock_s3 = self._make_client()
        internal_error = ClientError(
            {"Error": {"Code": "InternalError", "Message": "Server error"}},
            "PutObject",
        )
        mock_s3.put_object.side_effect = [internal_error, internal_error, None]

        key = client.upload_report("NVDA", "content", report_date="2026-01-01")
        self.assertEqual(key, "reports/2026-01-01/NVDA_report.md")
        self.assertEqual(mock_s3.put_object.call_count, 3)

    def test_non_retryable_error_raises_immediately(self):
        client, mock_s3 = self._make_client()
        access_denied = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}},
            "PutObject",
        )
        mock_s3.put_object.side_effect = access_denied

        with self.assertRaises(R2StorageError):
            client.upload_report("X", "data", report_date="2026-01-01")
        self.assertEqual(mock_s3.put_object.call_count, 1)


if __name__ == "__main__":
    unittest.main()
