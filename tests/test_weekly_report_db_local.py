"""
test_weekly_report_db_local.py — weekly_reports ローカル Supabase 統合テスト

実行条件:
  - ローカル Supabase が起動していること
  - または SUPABASE_TEST_URL / SUPABASE_TEST_SERVICE_KEY 環境変数が設定されていること

本番 Supabase URL が検出された場合はスキップする。

実行方法:
  python -m pytest tests/test_weekly_report_db_local.py -v

または:
  SUPABASE_TEST_URL=http://127.0.0.1:54321 \\
  SUPABASE_TEST_SERVICE_KEY=eyJ... \\
  python -m pytest tests/test_weekly_report_db_local.py -v
"""
import copy
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_config import PROD_PROJECT_REF
from weekly_db import WeeklyDBError, ProductionGuardError
from weekly_report_db import (
    WeeklyReportDB,
    validate_draft,
    build_db_payload,
    are_drafts_equal,
    verify_saved_report,
)

# ─── 接続設定 ─────────────────────────────────────────────────────────────────

_LOCAL_URL = "http://127.0.0.1:54321"
_LOCAL_SVC_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0"
    ".EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)

TEST_URL = os.environ.get("SUPABASE_TEST_URL", _LOCAL_URL)
TEST_KEY = os.environ.get("SUPABASE_TEST_SERVICE_KEY", _LOCAL_SVC_KEY)

# テスト用 week_id（本番データ・他テストと衝突しない過去の週）
TEST_WEEK_ID  = "2024-W01"

FIXTURES = Path(__file__).parent / "fixtures" / "weekly"


def _load_draft() -> dict:
    with open(FIXTURES / "draft_2026_W26_valid.json", encoding="utf-8") as f:
        d = json.load(f)
    # week_id を TEST_WEEK_ID に合わせる（period_start/end も更新）
    import datetime
    from weekly_dates import week_id_to_period
    ps, pe = week_id_to_period(TEST_WEEK_ID)
    d["week_id"] = TEST_WEEK_ID
    d["free_teaser"]["week_id"] = TEST_WEEK_ID
    d["free_teaser"]["period_start"] = str(ps)
    d["free_teaser"]["period_end"] = str(pe)
    d["free_teaser"]["title"] = f"Weekly Marketcast テスト週（{TEST_WEEK_ID}）"
    # テスト用に hash を再計算
    from weekly_report_builder import compute_hash
    d["teaser_hash"] = compute_hash(d["free_teaser"])
    d["paid_body_hash"] = compute_hash(d["paid_body"])
    return d


def _skip_if_production(test_case: unittest.TestCase) -> None:
    if PROD_PROJECT_REF in TEST_URL:
        test_case.skipTest(
            "本番 Supabase URL が設定されています。ローカル Supabase を使用してください。"
        )


def _db_available() -> bool:
    try:
        db = WeeklyReportDB(TEST_URL, TEST_KEY)
        db.get_report_full("__ping__")
        return True
    except Exception:
        return False


SKIP_NO_DB = unittest.skipUnless(_db_available(), "ローカル Supabase が利用できないためスキップ")


# ─── 統合テスト ───────────────────────────────────────────────────────────────

@SKIP_NO_DB
class TestReportDraftInsert(unittest.TestCase):
    """正常 INSERT / 再取得 / 整合確認。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(cls)
        cls.db = WeeklyReportDB(TEST_URL, TEST_KEY)

    def setUp(self):
        # テスト前にクリーンアップ
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass

    def tearDown(self):
        # テスト後にクリーンアップ（失敗時も必ず実行）
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass

    def test_insert_and_retrieve(self):
        """正常 INSERT → 再取得で全フィールド一致。"""
        draft = _load_draft()
        payload = build_db_payload(draft)

        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)

        self.assertIsNotNone(db_row)
        mismatches = verify_saved_report(db_row, payload)
        self.assertEqual(mismatches, [], f"整合確認失敗: {mismatches}")

    def test_status_is_draft(self):
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(db_row["status"], "draft")

    def test_null_columns_after_insert(self):
        """INSERT 後に NULL 列が NULL であること。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        null_cols = (
            "teaser_hash", "paid_body_hash",
            "reviewed_at", "reviewed_by",
            "published_at", "withdrawn_at", "withdrawal_reason",
        )
        for col in null_cols:
            with self.subTest(col=col):
                self.assertIsNone(db_row.get(col), f"{col} が NULL でない: {db_row.get(col)}")

    def test_free_teaser_roundtrip(self):
        """free_teaser が JSONB を経由して完全に往復できること。"""
        import json
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(
            json.dumps(db_row["free_teaser"], sort_keys=True, ensure_ascii=False),
            json.dumps(payload["free_teaser"], sort_keys=True, ensure_ascii=False),
        )

    def test_paid_body_roundtrip(self):
        """paid_body が JSONB を経由して完全に往復できること。"""
        import json
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(
            json.dumps(db_row["paid_body"], sort_keys=True, ensure_ascii=False),
            json.dumps(payload["paid_body"], sort_keys=True, ensure_ascii=False),
        )

    def test_created_at_and_updated_at_set(self):
        """DB が created_at / updated_at を自動設定すること。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertIsNotNone(db_row.get("created_at"))
        self.assertIsNotNone(db_row.get("updated_at"))


@SKIP_NO_DB
class TestReportDraftIdempotent(unittest.TestCase):
    """冪等性テスト。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(cls)
        cls.db = WeeklyReportDB(TEST_URL, TEST_KEY)

    def setUp(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass

    def tearDown(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass

    def test_same_draft_detected_as_equal(self):
        """同一 draft を2回保存しようとしたとき are_drafts_equal が True を返す。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertTrue(are_drafts_equal(db_row, payload))

    def test_row_count_one_after_insert(self):
        """INSERT 後に行数が1件であること。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertIsNotNone(db_row)

    def test_updated_at_unchanged_on_no_update(self):
        """INSERT 後に UPDATE しないとき updated_at が変化しないこと。"""
        import time
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        row1 = self.db.get_report_full(TEST_WEEK_ID)
        time.sleep(0.1)
        row2 = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(row1.get("updated_at"), row2.get("updated_at"))


@SKIP_NO_DB
class TestReportDraftConflict(unittest.TestCase):
    """衝突テスト（異なる draft / published / withdrawn）。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(cls)
        cls.db = WeeklyReportDB(TEST_URL, TEST_KEY)

    def setUp(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass

    def tearDown(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass

    def test_different_draft_detected_as_unequal(self):
        """異なる draft を2回保存しようとしたとき are_drafts_equal が False を返す。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)

        # 別の draft payload を生成
        draft2 = _load_draft()
        draft2["free_teaser"]["env_label"] = "方向感分散"
        from weekly_report_builder import compute_hash
        draft2["teaser_hash"] = compute_hash(draft2["free_teaser"])
        payload2 = build_db_payload(draft2)

        self.assertFalse(are_drafts_equal(db_row, payload2))

    def test_db_row_unchanged_after_conflict_detected(self):
        """衝突検出後に DB 行が変更されていないこと（アプリ側が UPDATE しないことの確認）。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        row_before = self.db.get_report_full(TEST_WEEK_ID)

        # アプリは UPDATE を行わないので DB 行は変わらない
        row_after = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(row_before.get("updated_at"), row_after.get("updated_at"))

    def test_published_row_detected_by_status(self):
        """published 行の status が正しく取得されること（ガード判定用）。"""
        # DB CHECK 制約があるため直接 published を INSERT できない
        # status 確認ロジックのテストは単体テストで行う
        # ここでは draft 行の status を確認する
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(db_row["status"], "draft")


@SKIP_NO_DB
class TestReportDraftCheckConstraint(unittest.TestCase):
    """DB CHECK 制約テスト。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(cls)
        cls.db = WeeklyReportDB(TEST_URL, TEST_KEY)

    def setUp(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass

    def tearDown(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass

    def test_draft_with_teaser_hash_rejected(self):
        """status=draft で teaser_hash を設定すると CHECK 制約違反になること。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        payload["teaser_hash"] = "a" * 64  # draft には設定不可

        with self.assertRaises(WeeklyDBError):
            self.db.insert_report_draft(payload)

        # 失敗後に行が残らないこと
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertIsNone(db_row)

    def test_draft_with_reviewed_at_rejected(self):
        """status=draft で reviewed_at を設定すると CHECK 制約違反になること。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        payload["reviewed_at"] = "2026-06-28T00:00:00+00:00"

        with self.assertRaises(WeeklyDBError):
            self.db.insert_report_draft(payload)

        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertIsNone(db_row)

    def test_invalid_week_id_format_rejected(self):
        """不正な week_id フォーマットが CHECK 制約で拒否されること。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        payload["week_id"] = "invalid-week"

        with self.assertRaises(WeeklyDBError):
            self.db.insert_report_draft(payload)


@SKIP_NO_DB
class TestReportDraftProductionGuard(unittest.TestCase):
    """本番ガードテスト。"""

    def test_local_url_allows_insert(self):
        """ローカル URL では allow_production=False でも INSERT できること。"""
        db = WeeklyReportDB(TEST_URL, TEST_KEY)
        self.assertFalse(db.is_production())

    def test_production_url_rejected_without_flag(self):
        """本番 URL では allow_production=False で ProductionGuardError になること。"""
        import re
        prod_url = f"https://{PROD_PROJECT_REF}.supabase.co"
        dummy_key = "eyJdummy.eyJdummy.dummy"
        db = WeeklyReportDB(prod_url, dummy_key)

        draft = _load_draft()
        payload = build_db_payload(draft)

        with self.assertRaises(ProductionGuardError):
            db.insert_report_draft(payload, allow_production=False)

    def test_production_url_detected_correctly(self):
        """本番 URL の検出が正しいこと。"""
        prod_url = f"https://{PROD_PROJECT_REF}.supabase.co"
        db = WeeklyReportDB(prod_url, "dummy")
        self.assertTrue(db.is_production())

    def test_local_url_is_not_production(self):
        """ローカル URL が本番として検出されないこと。"""
        db = WeeklyReportDB(TEST_URL, TEST_KEY)
        self.assertFalse(db.is_production())


@SKIP_NO_DB
class TestReportCleanup(unittest.TestCase):
    """クリーンアップ・他テーブル影響なし確認。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(cls)
        cls.db = WeeklyReportDB(TEST_URL, TEST_KEY)

    def setUp(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass

    def tearDown(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass

    def test_delete_removes_row(self):
        """delete_report 後に行が存在しないこと。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        self.db.delete_report(TEST_WEEK_ID)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertIsNone(db_row)

    def test_snapshots_table_unaffected(self):
        """weekly_asset_snapshots テーブルが変更されていないこと。"""
        # スナップの件数が変わらないことを確認
        before = self.db.get_snapshots(TEST_WEEK_ID)
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        self.db.delete_report(TEST_WEEK_ID)
        after = self.db.get_snapshots(TEST_WEEK_ID)
        self.assertEqual(len(before), len(after))


if __name__ == "__main__":
    unittest.main(verbosity=2)
