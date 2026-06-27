"""
test_weekly_report_db.py — weekly_report_db.py の単体テスト（DB 接続不要）

テスト対象:
  - validate_draft(): 保存前検証
  - build_db_payload(): DB payload 構築
  - are_drafts_equal(): 冪等性比較
  - verify_saved_report(): 保存後整合確認
"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_report_db import (
    validate_draft,
    build_db_payload,
    are_drafts_equal,
    verify_saved_report,
    _canonical,
    _datetimes_equal,
)

FIXTURES = Path(__file__).parent / "fixtures" / "weekly"


def _load_draft() -> dict:
    """有効な draft fixture を返す（deep copy）。"""
    with open(FIXTURES / "draft_2026_W26_valid.json", encoding="utf-8") as f:
        return json.load(f)


# ─── validate_draft テスト ───────────────────────────────────────────────────

class TestValidateDraftValid(unittest.TestCase):

    def test_valid_draft_no_errors(self):
        draft = _load_draft()
        errors = validate_draft(draft, "2026-W26")
        self.assertEqual(errors, [], f"エラーが発生: {errors}")

    def test_warnings_do_not_cause_errors(self):
        draft = _load_draft()
        draft["warnings"] = ["[WARN] テスト警告"]
        errors = validate_draft(draft, "2026-W26")
        self.assertEqual(errors, [])

    def test_empty_warnings_ok(self):
        draft = _load_draft()
        draft["warnings"] = []
        errors = validate_draft(draft, "2026-W26")
        self.assertEqual(errors, [])


class TestValidateDraftWeekId(unittest.TestCase):

    def test_week_id_mismatch(self):
        draft = _load_draft()
        errors = validate_draft(draft, "2026-W25")
        self.assertTrue(any("week_id" in e for e in errors), errors)

    def test_week_id_none(self):
        draft = _load_draft()
        draft["week_id"] = None
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("week_id" in e for e in errors), errors)


class TestValidateDraftRevision(unittest.TestCase):

    def test_revision_zero_fails(self):
        draft = _load_draft()
        draft["revision"] = 0
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("revision" in e for e in errors), errors)

    def test_revision_negative_fails(self):
        draft = _load_draft()
        draft["revision"] = -1
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("revision" in e for e in errors), errors)

    def test_revision_string_fails(self):
        draft = _load_draft()
        draft["revision"] = "1"
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("revision" in e for e in errors), errors)

    def test_revision_one_ok(self):
        draft = _load_draft()
        draft["revision"] = 1
        errors = validate_draft(draft, "2026-W26")
        self.assertEqual(errors, [])


class TestValidateDraftGeneratedAt(unittest.TestCase):

    def test_invalid_generated_at(self):
        draft = _load_draft()
        draft["generated_at"] = "not-a-datetime"
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("generated_at" in e for e in errors), errors)

    def test_none_generated_at(self):
        draft = _load_draft()
        draft["generated_at"] = None
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("generated_at" in e for e in errors), errors)


class TestValidateDraftSchema(unittest.TestCase):

    def test_free_teaser_schema_violation(self):
        draft = _load_draft()
        draft["free_teaser"] = {"bad_field": "value"}
        # hash も更新しないので hash 不一致エラーが先に出るが、どちらかが検出されればOK
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(len(errors) > 0, "スキーマ違反が検出されなかった")

    def test_paid_body_schema_violation(self):
        draft = _load_draft()
        draft["paid_body"] = {"summary": "x"}
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(len(errors) > 0, "スキーマ違反が検出されなかった")


class TestValidateDraftHash(unittest.TestCase):

    def test_teaser_hash_mismatch(self):
        draft = _load_draft()
        draft["teaser_hash"] = "a" * 64
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("teaser_hash" in e for e in errors), errors)

    def test_paid_body_hash_mismatch(self):
        draft = _load_draft()
        draft["paid_body_hash"] = "b" * 64
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("paid_body_hash" in e for e in errors), errors)

    def test_hash_wrong_length(self):
        draft = _load_draft()
        draft["teaser_hash"] = "abc"
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("teaser_hash" in e for e in errors), errors)

    def test_hash_uppercase_fails(self):
        draft = _load_draft()
        draft["teaser_hash"] = "A" * 64
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("teaser_hash" in e for e in errors), errors)


class TestValidateDraftHardErrors(unittest.TestCase):

    def test_hard_errors_present(self):
        draft = _load_draft()
        draft["hard_errors"] = ["[HARD] テストエラー"]
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("hard_errors" in e for e in errors), errors)

    def test_empty_hard_errors_ok(self):
        draft = _load_draft()
        draft["hard_errors"] = []
        errors = validate_draft(draft, "2026-W26")
        self.assertEqual(errors, [])


class TestValidateDraftRestrictedLeak(unittest.TestCase):

    def test_forbidden_key_in_paid_body(self):
        draft = _load_draft()
        # 禁止キー "api_key" を paid_body に注入
        draft["paid_body"]["api_key"] = "should-not-be-here"
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(len(errors) > 0, "禁止キーが検出されなかった")

    def test_paid_info_in_free_teaser(self):
        draft = _load_draft()
        # free_teaser に score（有料情報）を注入
        draft["free_teaser"]["score"] = 3
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(len(errors) > 0, "paid情報混入が検出されなかった")


class TestValidateDraftPeriod(unittest.TestCase):

    def test_period_start_mismatch(self):
        draft = _load_draft()
        draft["free_teaser"]["period_start"] = "2026-06-01"
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("period_start" in e for e in errors), errors)

    def test_period_end_mismatch(self):
        draft = _load_draft()
        draft["free_teaser"]["period_end"] = "2026-06-30"
        errors = validate_draft(draft, "2026-W26")
        self.assertTrue(any("period_end" in e for e in errors), errors)


# ─── build_db_payload テスト ─────────────────────────────────────────────────

class TestBuildDbPayload(unittest.TestCase):

    def setUp(self):
        self.draft = _load_draft()
        self.payload = build_db_payload(self.draft)

    def test_status_is_draft(self):
        self.assertEqual(self.payload["status"], "draft")

    def test_teaser_hash_is_null(self):
        self.assertIsNone(self.payload["teaser_hash"])

    def test_paid_body_hash_is_null(self):
        self.assertIsNone(self.payload["paid_body_hash"])

    def test_reviewed_at_is_null(self):
        self.assertIsNone(self.payload["reviewed_at"])

    def test_reviewed_by_is_null(self):
        self.assertIsNone(self.payload["reviewed_by"])

    def test_published_at_is_null(self):
        self.assertIsNone(self.payload["published_at"])

    def test_withdrawn_at_is_null(self):
        self.assertIsNone(self.payload["withdrawn_at"])

    def test_withdrawal_reason_is_null(self):
        self.assertIsNone(self.payload["withdrawal_reason"])

    def test_no_created_at(self):
        self.assertNotIn("created_at", self.payload)

    def test_no_updated_at(self):
        self.assertNotIn("updated_at", self.payload)

    def test_free_teaser_matches(self):
        self.assertEqual(
            _canonical(self.payload["free_teaser"]),
            _canonical(self.draft["free_teaser"]),
        )

    def test_paid_body_matches(self):
        self.assertEqual(
            _canonical(self.payload["paid_body"]),
            _canonical(self.draft["paid_body"]),
        )

    def test_revision_matches(self):
        self.assertEqual(self.payload["revision"], self.draft["revision"])

    def test_week_id_matches(self):
        self.assertEqual(self.payload["week_id"], self.draft["week_id"])

    def test_title_from_free_teaser(self):
        self.assertEqual(
            self.payload["title"],
            self.draft["free_teaser"]["title"],
        )

    def test_period_start_from_free_teaser(self):
        self.assertEqual(
            self.payload["period_start"],
            self.draft["free_teaser"]["period_start"],
        )

    def test_period_end_from_free_teaser(self):
        self.assertEqual(
            self.payload["period_end"],
            self.draft["free_teaser"]["period_end"],
        )


# ─── are_drafts_equal テスト ─────────────────────────────────────────────────

class TestAreDraftsEqual(unittest.TestCase):

    def _make_db_row(self, payload: dict) -> dict:
        """payload から DB 行を模擬する（created_at / updated_at を追加）。"""
        row = copy.deepcopy(payload)
        row["created_at"] = "2026-06-27T00:00:01+00:00"
        row["updated_at"] = "2026-06-27T00:00:01+00:00"
        return row

    def test_equal_same_payload(self):
        draft = _load_draft()
        payload = build_db_payload(draft)
        db_row = self._make_db_row(payload)
        self.assertTrue(are_drafts_equal(db_row, payload))

    def test_different_generated_at(self):
        draft = _load_draft()
        payload = build_db_payload(draft)
        db_row = self._make_db_row(payload)
        db_row["generated_at"] = "2026-06-28T00:00:00+00:00"
        self.assertFalse(are_drafts_equal(db_row, payload))

    def test_different_free_teaser(self):
        draft = _load_draft()
        payload = build_db_payload(draft)
        db_row = self._make_db_row(payload)
        db_row["free_teaser"]["env_label"] = "変更後ラベル"
        self.assertFalse(are_drafts_equal(db_row, payload))

    def test_different_revision(self):
        draft = _load_draft()
        payload = build_db_payload(draft)
        db_row = self._make_db_row(payload)
        db_row["revision"] = 2
        self.assertFalse(are_drafts_equal(db_row, payload))

    def test_generated_at_utc_equivalence(self):
        """UTC 表記の違い（Z vs +00:00）を同一とみなす。"""
        draft = _load_draft()
        payload = build_db_payload(draft)
        db_row = self._make_db_row(payload)
        # DB が Z 形式で返してきた場合を模擬
        db_row["generated_at"] = draft["generated_at"].replace("+00:00", "Z")
        self.assertTrue(are_drafts_equal(db_row, payload))


# ─── verify_saved_report テスト ──────────────────────────────────────────────

class TestVerifySavedReport(unittest.TestCase):

    def _make_db_row(self, payload: dict) -> dict:
        row = copy.deepcopy(payload)
        row["created_at"] = "2026-06-27T00:00:01+00:00"
        row["updated_at"] = "2026-06-27T00:00:01+00:00"
        return row

    def setUp(self):
        draft = _load_draft()
        self.payload = build_db_payload(draft)
        self.db_row  = self._make_db_row(self.payload)

    def test_no_mismatches_on_valid(self):
        mismatches = verify_saved_report(self.db_row, self.payload)
        self.assertEqual(mismatches, [], f"不一致: {mismatches}")

    def test_detects_non_null_teaser_hash(self):
        self.db_row["teaser_hash"] = "a" * 64
        mismatches = verify_saved_report(self.db_row, self.payload)
        self.assertTrue(any("teaser_hash" in m for m in mismatches), mismatches)

    def test_detects_non_null_paid_body_hash(self):
        self.db_row["paid_body_hash"] = "b" * 64
        mismatches = verify_saved_report(self.db_row, self.payload)
        self.assertTrue(any("paid_body_hash" in m for m in mismatches), mismatches)

    def test_detects_wrong_status(self):
        self.db_row["status"] = "published"
        mismatches = verify_saved_report(self.db_row, self.payload)
        self.assertTrue(any("status" in m for m in mismatches), mismatches)

    def test_detects_reviewed_at_not_null(self):
        self.db_row["reviewed_at"] = "2026-06-28T00:00:00+00:00"
        mismatches = verify_saved_report(self.db_row, self.payload)
        self.assertTrue(any("reviewed_at" in m for m in mismatches), mismatches)

    def test_detects_free_teaser_mismatch(self):
        self.db_row["free_teaser"]["env_label"] = "変更後"
        mismatches = verify_saved_report(self.db_row, self.payload)
        self.assertTrue(any("free_teaser" in m for m in mismatches), mismatches)

    def test_detects_week_id_mismatch(self):
        self.db_row["week_id"] = "2026-W99"
        mismatches = verify_saved_report(self.db_row, self.payload)
        self.assertTrue(any("week_id" in m for m in mismatches), mismatches)


# ─── _datetimes_equal テスト ─────────────────────────────────────────────────

class TestDatetimesEqual(unittest.TestCase):

    def test_same_string(self):
        dt = "2026-06-27T00:00:00+00:00"
        self.assertTrue(_datetimes_equal(dt, dt))

    def test_z_vs_offset(self):
        self.assertTrue(
            _datetimes_equal("2026-06-27T00:00:00Z", "2026-06-27T00:00:00+00:00")
        )

    def test_different_times(self):
        self.assertFalse(
            _datetimes_equal("2026-06-27T00:00:00Z", "2026-06-27T01:00:00+00:00")
        )

    def test_both_none(self):
        self.assertTrue(_datetimes_equal(None, None))

    def test_one_none(self):
        self.assertFalse(_datetimes_equal("2026-06-27T00:00:00Z", None))
        self.assertFalse(_datetimes_equal(None, "2026-06-27T00:00:00Z"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
