"""
test_weekly_publish_db_local.py — published 遷移のローカル Supabase 統合テスト

実行条件:
  - ローカル Supabase が起動していること
  - または SUPABASE_TEST_URL / SUPABASE_TEST_SERVICE_KEY 環境変数が設定されていること

本番 Supabase URL が検出された場合はスキップする。

実行方法:
  python -m pytest tests/test_weekly_publish_db_local.py -v
"""
import copy
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_config import PROD_PROJECT_REF
from weekly_db import WeeklyDBError
from weekly_dates import week_id_to_period
from weekly_report_builder import compute_hash
from weekly_report_db import WeeklyReportDB, build_db_payload, validate_draft
from weekly_report_publish import (
    verify_pre_publish,
    build_publish_payload,
    verify_published_report,
    are_published_idempotent,
    apply_published_transition,
)
from weekly_approval import (
    build_approval_payload,
    validate_approval_schema,
    verify_db_draft_matches_local,
    are_approvals_equal,
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

# W2-3 の 2024-W01 と衝突しない過去の週
TEST_WEEK_ID = "2023-W01"

FIXTURES = Path(__file__).parent / "fixtures" / "weekly"
_APPROVED_AT  = "2023-01-10T01:00:00+00:00"
_PUBLISHED_AT = "2023-01-10T02:00:00+00:00"


def _load_draft() -> dict:
    with open(FIXTURES / "draft_2026_W26_valid.json", encoding="utf-8") as f:
        d = json.load(f)
    ps, pe = week_id_to_period(TEST_WEEK_ID)
    d["week_id"] = TEST_WEEK_ID
    d["free_teaser"]["week_id"]      = TEST_WEEK_ID
    d["free_teaser"]["period_start"] = str(ps)
    d["free_teaser"]["period_end"]   = str(pe)
    d["free_teaser"]["title"]        = f"Weekly Marketcast テスト（{TEST_WEEK_ID}）"
    d["teaser_hash"]   = compute_hash(d["free_teaser"])
    d["paid_body_hash"] = compute_hash(d["paid_body"])
    return d


def _build_approval(draft: dict) -> dict:
    return build_approval_payload(
        TEST_WEEK_ID, draft, "test-operator", _APPROVED_AT
    )


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


# ─── draft → approval → published 正常フロー ─────────────────────────────────

@SKIP_NO_DB
class TestPublishHappyPath(unittest.TestCase):
    """draft 行を INSERT → 承認 → published 遷移 → 整合確認。"""

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

    def _insert_draft(self) -> tuple[dict, dict]:
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        return draft, db_row

    def test_db_draft_matches_local(self):
        draft, db_row = self._insert_draft()
        errors = verify_db_draft_matches_local(db_row, draft)
        self.assertEqual(errors, [], f"照合エラー: {errors}")

    def test_pre_publish_verify_ok(self):
        draft, db_row = self._insert_draft()
        approval = _build_approval(draft)
        errors = verify_pre_publish(approval, draft, db_row)
        self.assertEqual(errors, [], f"公開前検証エラー: {errors}")

    def test_apply_published_transition_returns_row(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        result = apply_published_transition(self.db, TEST_WEEK_ID, approval)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "published")

    def test_status_published_after_patch(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(db_row["status"], "published")

    def test_teaser_hash_set_after_publish(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(db_row["teaser_hash"], approval["teaser_hash"])

    def test_paid_body_hash_set_after_publish(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(db_row["paid_body_hash"], approval["paid_body_hash"])

    def test_reviewed_at_set_after_publish(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertIsNotNone(db_row["reviewed_at"])

    def test_reviewed_by_set_after_publish(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(db_row["reviewed_by"], "test-operator")

    def test_published_at_set_after_publish(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertIsNotNone(db_row["published_at"])

    def test_withdrawn_at_null_after_publish(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertIsNone(db_row["withdrawn_at"])

    def test_free_teaser_unchanged_after_publish(self):
        """PATCH 後に free_teaser が変わっていないこと。"""
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        computed = compute_hash(db_row["free_teaser"])
        self.assertEqual(computed, approval["teaser_hash"])

    def test_paid_body_unchanged_after_publish(self):
        """PATCH 後に paid_body が変わっていないこと。"""
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        computed = compute_hash(db_row["paid_body"])
        self.assertEqual(computed, approval["paid_body_hash"])

    def test_revision_unchanged_after_publish(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        self.assertEqual(db_row["revision"], draft["revision"])

    def test_verify_published_report_passes(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        result = apply_published_transition(self.db, TEST_WEEK_ID, approval)
        payload = build_publish_payload(approval, result["published_at"])
        mismatches = verify_published_report(result, payload, approval)
        self.assertEqual(mismatches, [], f"整合確認失敗: {mismatches}")


# ─── 既存 draft なし → PATCH 失敗 ────────────────────────────────────────────

@SKIP_NO_DB
class TestPublishNoRow(unittest.TestCase):
    """draft 行が存在しない場合 PATCH が 0 件になること。"""

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

    def test_patch_without_draft_row_raises(self):
        draft = _load_draft()
        approval = _build_approval(draft)
        with self.assertRaises(WeeklyDBError):
            apply_published_transition(self.db, TEST_WEEK_ID, approval)


# ─── revision 不一致 → PATCH 失敗 ────────────────────────────────────────────

@SKIP_NO_DB
class TestPublishRevisionMismatch(unittest.TestCase):
    """承認ファイルの revision が DB と異なる場合 PATCH が 0 件になること。"""

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

    def test_wrong_revision_raises(self):
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)

        approval = _build_approval(draft)
        approval_wrong = copy.deepcopy(approval)
        approval_wrong["revision"] = 99  # DB には revision=1 が入っている

        with self.assertRaises(WeeklyDBError):
            apply_published_transition(self.db, TEST_WEEK_ID, approval_wrong)


# ─── 冪等性チェック（published 済み行） ──────────────────────────────────────

@SKIP_NO_DB
class TestPublishIdempotent(unittest.TestCase):
    """公開済み行に対して are_published_idempotent が正しく動作すること。"""

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

    def test_same_approval_is_idempotent(self):
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        approval = _build_approval(draft)
        result = apply_published_transition(self.db, TEST_WEEK_ID, approval)

        pub_payload = build_publish_payload(approval, result["published_at"])
        self.assertTrue(
            are_published_idempotent(result, pub_payload, approval),
            "同一内容が冪等判定されなかった",
        )

    def test_different_operator_is_not_idempotent(self):
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        approval = _build_approval(draft)
        result = apply_published_transition(self.db, TEST_WEEK_ID, approval)

        approval2 = copy.deepcopy(approval)
        approval2["approved_by"] = "other-operator"
        pub_payload2 = build_publish_payload(approval2, result["published_at"])

        self.assertFalse(
            are_published_idempotent(result, pub_payload2, approval2),
            "異なる operator が冪等と判定された",
        )

    def test_second_patch_returns_zero_rows(self):
        """
        既に published の行に status=eq.draft 条件で PATCH すると 0 件になること。
        これは PATCH が条件付きであるため、published 行には適用されない。
        """
        draft = _load_draft()
        db_payload = build_db_payload(draft)
        self.db.insert_report_draft(db_payload)
        approval = _build_approval(draft)
        apply_published_transition(self.db, TEST_WEEK_ID, approval)

        pub_payload = build_publish_payload(approval, _PUBLISHED_AT)
        rows = self.db.patch_report_to_published(
            TEST_WEEK_ID, approval["revision"], pub_payload
        )
        self.assertEqual(len(rows), 0, "published 行への再 PATCH が 0 件でないかった")


# ─── 本番ガード確認 ───────────────────────────────────────────────────────────

@SKIP_NO_DB
class TestPublishProductionGuard(unittest.TestCase):
    """patch_report_to_published が本番 URL を拒否すること。"""

    def test_production_url_rejected(self):
        from weekly_db import ProductionGuardError
        prod_url = f"https://{PROD_PROJECT_REF}.supabase.co"
        db = WeeklyReportDB(prod_url, "dummy-key")
        draft = _load_draft()
        approval = _build_approval(draft)
        pub_payload = build_publish_payload(approval, _PUBLISHED_AT)

        with self.assertRaises(ProductionGuardError):
            db.patch_report_to_published(
                TEST_WEEK_ID, approval["revision"], pub_payload, allow_production=False
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
