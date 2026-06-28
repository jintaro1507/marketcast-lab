"""
tests/test_weekly_sources.py — weekly_sources の単体テスト

ネットワーク不要。stdlib unittest + unittest.mock のみ使用。
"""
import contextlib
import datetime
import io
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_sources import classify_stooq_error, fetch_week_observations

PERIOD_START = datetime.date(2026, 6, 15)
PERIOD_END   = datetime.date(2026, 6, 19)
FRED_KEY     = "test_fred_key"
FAKE_YAHOO   = [("2026-06-18", 180.0)]  # 週内木曜値

STOOQ_CFG = {
    "asset_key":    "gold",
    "source":       "stooq",
    "stooq_id":     "gld.us",
    "yahoo_symbol": "GLD",
}


class TestClassifyStooqError(unittest.TestCase):
    """classify_stooq_error の分類ロジックを単体テスト。"""

    def test_html_response(self):
        exc = RuntimeError("Stooqにブロックされました（HTML応答）")
        self.assertEqual(classify_stooq_error(exc), "stooq_html_response")

    def test_invalid_csv_header(self):
        exc = RuntimeError("有効なCSVヘッダなし（先頭: '<html>...'）")
        self.assertEqual(classify_stooq_error(exc), "stooq_invalid_csv_header")

    def test_empty_data_rows(self):
        exc = RuntimeError("CSVは取得したがデータ行が空")
        self.assertEqual(classify_stooq_error(exc), "stooq_empty_data_rows")

    def test_http_error_429(self):
        exc = HTTPError("https://stooq.com/q/d/l/", 429, "Too Many Requests", {}, None)
        self.assertEqual(classify_stooq_error(exc), "stooq_http_error:429")

    def test_http_error_403(self):
        exc = HTTPError("https://stooq.com/q/d/l/", 403, "Forbidden", {}, None)
        self.assertEqual(classify_stooq_error(exc), "stooq_http_error:403")

    def test_url_error(self):
        exc = URLError("connection refused")
        self.assertEqual(classify_stooq_error(exc), "stooq_connection_error")

    def test_url_error_timeout(self):
        import socket
        exc = URLError(socket.timeout("timed out"))
        self.assertEqual(classify_stooq_error(exc), "stooq_connection_error")

    def test_unknown_runtime_error(self):
        exc = RuntimeError("completely unexpected message")
        self.assertEqual(classify_stooq_error(exc), "stooq_unknown_error")

    def test_value_error(self):
        exc = ValueError("not a URLError or RuntimeError")
        self.assertEqual(classify_stooq_error(exc), "stooq_unknown_error")

    def test_generic_exception(self):
        exc = Exception("generic")
        self.assertEqual(classify_stooq_error(exc), "stooq_unknown_error")

    def test_http_error_is_subclass_of_url_error(self):
        # HTTPError は URLError のサブクラス。HTTPError を先にチェックする。
        exc = HTTPError("https://stooq.com/q/d/l/", 500, "Server Error", {}, None)
        result = classify_stooq_error(exc)
        self.assertTrue(result.startswith("stooq_http_error:"), result)


class TestFallbackLogSafety(unittest.TestCase):
    """Stooq fallback ログが例外本文・response断片を含まないことを確認する。"""

    def _run_capture(self, stooq_exc, yahoo_data=None):
        """Stooqが stooq_exc を投げ Yahoo が yahoo_data を返す場合の stdout を返す。"""
        import fetch_and_build as fab
        data = yahoo_data if yahoo_data is not None else FAKE_YAHOO
        buf = io.StringIO()
        with mock.patch.object(fab, "fetch_stooq_series", side_effect=stooq_exc), \
             mock.patch.object(fab, "fetch_yahoo_series", return_value=data), \
             contextlib.redirect_stdout(buf):
            try:
                fetch_week_observations(STOOQ_CFG, FRED_KEY, PERIOD_START, PERIOD_END)
            except Exception:
                pass
        return buf.getvalue()

    def test_html_response_shows_safe_code(self):
        exc = RuntimeError("Stooqにブロックされました（HTML応答）")
        out = self._run_capture(exc)
        self.assertIn("stooq_html_response", out)

    def test_html_response_no_exception_message(self):
        exc = RuntimeError("Stooqにブロックされました（HTML応答）")
        out = self._run_capture(exc)
        self.assertNotIn("ブロックされました", out)
        self.assertNotIn("HTML応答", out)

    def test_invalid_header_shows_safe_code(self):
        exc = RuntimeError("有効なCSVヘッダなし（先頭: 'abc'）")
        out = self._run_capture(exc)
        self.assertIn("stooq_invalid_csv_header", out)

    def test_invalid_header_no_response_fragment(self):
        exc = RuntimeError("有効なCSVヘッダなし（先頭: '<html>some_content'）")
        out = self._run_capture(exc)
        self.assertNotIn("先頭", out)
        self.assertNotIn("<html>", out)
        self.assertNotIn("some_content", out)

    def test_raw_price_not_in_log(self):
        exc = RuntimeError("有効なCSVヘッダなし（先頭: '1900.12,1901.34'）")
        out = self._run_capture(exc)
        self.assertNotIn("1900.12", out)
        self.assertNotIn("1901.34", out)

    def test_api_key_in_exception_not_in_log(self):
        exc = RuntimeError("有効なCSVヘッダなし（先頭: 'api_key=secret_abc123'）")
        out = self._run_capture(exc)
        self.assertNotIn("secret_abc123", out)
        self.assertNotIn("api_key=", out)

    def test_empty_data_rows_shows_safe_code(self):
        exc = RuntimeError("CSVは取得したがデータ行が空")
        out = self._run_capture(exc)
        self.assertIn("stooq_empty_data_rows", out)

    def test_empty_data_rows_no_exception_message(self):
        exc = RuntimeError("CSVは取得したがデータ行が空")
        out = self._run_capture(exc)
        self.assertNotIn("データ行が空", out)

    def test_http_error_safe_code_only(self):
        exc = HTTPError("https://stooq.com/q/d/l/", 429, "Too Many Requests", {}, None)
        out = self._run_capture(exc)
        self.assertIn("stooq_http_error:429", out)
        self.assertNotIn("Too Many Requests", out)

    def test_url_error_safe_code(self):
        exc = URLError("connection refused")
        out = self._run_capture(exc)
        self.assertIn("stooq_connection_error", out)
        self.assertNotIn("connection refused", out)

    def test_unknown_runtime_error_safe_code(self):
        exc = RuntimeError("some unknown error message")
        out = self._run_capture(exc)
        self.assertIn("stooq_unknown_error", out)
        self.assertNotIn("unknown error message", out)

    def test_no_type_name_in_log(self):
        # 旧コードは type(e).__name__ を出力していた。新コードは出力しない。
        exc = RuntimeError("Stooqにブロックされました（HTML応答）")
        out = self._run_capture(exc)
        self.assertNotIn("RuntimeError", out)

    def test_yahoo_called_once_after_stooq_failure(self):
        import fetch_and_build as fab
        exc = RuntimeError("Stooqにブロックされました（HTML応答）")
        buf = io.StringIO()
        with mock.patch.object(fab, "fetch_stooq_series", side_effect=exc) as m_stooq, \
             mock.patch.object(fab, "fetch_yahoo_series", return_value=FAKE_YAHOO) as m_yahoo, \
             contextlib.redirect_stdout(buf):
            fetch_week_observations(STOOQ_CFG, FRED_KEY, PERIOD_START, PERIOD_END)
        m_stooq.assert_called_once()
        m_yahoo.assert_called_once()

    def test_stooq_id_appears_in_log(self):
        # stooq_id はログに含まれる（資産特定のため）
        exc = RuntimeError("Stooqにブロックされました（HTML応答）")
        out = self._run_capture(exc)
        self.assertIn("gld.us", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
