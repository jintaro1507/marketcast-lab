"""
test_weekly_approval.py — weekly_approval.py の単体テスト（DB 接続不要）

テスト対象:
  - get_operator_name(): OPERATOR_NAME 環境変数取得
  - build_approval_payload(): 承認 payload 構築
  - validate_approval_schema(): スキーマ検証
  - are_approvals_equal(): 冪等性比較
  - parse_approval_input(): 承認入力パース
  - load_approval() / save_approval(): ファイル I/O
  - verify_db_draft_matches_local(): DB draft 照合
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_approval import (
    get_operator_name,
    build_approval_payload,
    validate_approval_schema,
    are_approvals_equal,
    load_approval,
    save_approval,
    verify_db_draft_matches_local,
    parse_approval_input,
    OperatorNameError,
    ApprovalFileError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "weekly"

_VALID_HASH_64 = "fdc609afb24d11c1f0782bf5361d93c44bdccbc0e7cacdc469e714c8afbcecd5"
_VALID_HASH2_64 = "3af1820138cedd6348c459806a74024d2826f38bb9196a15c1735cedaa3de0d9"


def _load_fixture_approval() -> dict:
    with open(FIXTURES / "approval_2026_W26_valid.json", encoding="utf-8") as f:
        return json.load(f)


def _load_fixture_draft() -> dict:
    with open(FIXTURES / "draft_2026_W26_valid.json", encoding="utf-8") as f:
        return json.load(f)


def _make_db_row(draft: dict) -> dict:
    """draft から DB draft 行を模擬する（teaser_hash / paid_body_hash は NULL）。"""
    return {
        "week_id":           draft["week_id"],
        "status":            "draft",
        "revision":          draft["revision"],
        "generated_at":      draft["generated_at"],
        "free_teaser":       draft["free_teaser"],
        "paid_body":         draft["paid_body"],
        "teaser_hash":       None,
        "paid_body_hash":    None,
        "reviewed_at":       None,
        "reviewed_by":       None,
        "published_at":      None,
        "withdrawn_at":      None,
        "withdrawal_reason": None,
        "created_at":        "2026-06-27T00:00:01+00:00",
        "updated_at":        "2026-06-27T00:00:01+00:00",
    }


# ─── get_operator_name ───────────────────────────────────────────────────────

class TestGetOperatorName(unittest.TestCase):

    def setUp(self):
        self._orig = os.environ.pop("OPERATOR_NAME", None)

    def tearDown(self):
        os.environ.pop("OPERATOR_NAME", None)
        if self._orig is not None:
            os.environ["OPERATOR_NAME"] = self._orig

    def test_returns_name_from_env(self):
        os.environ["OPERATOR_NAME"] = "test-operator"
        self.assertEqual(get_operator_name(), "test-operator")

    def test_raises_when_not_set(self):
        with self.assertRaises(OperatorNameError):
            get_operator_name()

    def test_raises_when_empty(self):
        os.environ["OPERATOR_NAME"] = ""
        with self.assertRaises(OperatorNameError):
            get_operator_name()

    def test_raises_when_too_long(self):
        os.environ["OPERATOR_NAME"] = "a" * 65
        with self.assertRaises(OperatorNameError):
            get_operator_name()

    def test_exactly_64_chars_ok(self):
        os.environ["OPERATOR_NAME"] = "a" * 64
        self.assertEqual(len(get_operator_name()), 64)

    def test_one_char_ok(self):
        os.environ["OPERATOR_NAME"] = "x"
        self.assertEqual(get_operator_name(), "x")


# ─── build_approval_payload ──────────────────────────────────────────────────

class TestBuildApprovalPayload(unittest.TestCase):

    def setUp(self):
        self.draft = _load_fixture_draft()
        self.now = "2026-06-29T01:00:00+00:00"
        self.approval = build_approval_payload(
            "2026-W26", self.draft, "test-operator", self.now
        )

    def test_week_id(self):
        self.assertEqual(self.approval["week_id"], "2026-W26")

    def test_revision(self):
        self.assertEqual(self.approval["revision"], self.draft["revision"])

    def test_approved_at(self):
        self.assertEqual(self.approval["approved_at"], self.now)

    def test_approved_by(self):
        self.assertEqual(self.approval["approved_by"], "test-operator")

    def test_draft_generated_at(self):
        self.assertEqual(self.approval["draft_generated_at"], self.draft["generated_at"])

    def test_teaser_hash(self):
        self.assertEqual(self.approval["teaser_hash"], self.draft["teaser_hash"])

    def test_paid_body_hash(self):
        self.assertEqual(self.approval["paid_body_hash"], self.draft["paid_body_hash"])

    def test_approval_version(self):
        self.assertEqual(self.approval["approval_version"], 1)

    def test_no_free_teaser_in_payload(self):
        self.assertNotIn("free_teaser", self.approval)

    def test_no_paid_body_in_payload(self):
        self.assertNotIn("paid_body", self.approval)

    def test_exactly_eight_keys(self):
        self.assertEqual(len(self.approval), 8)


# ─── validate_approval_schema ────────────────────────────────────────────────

class TestValidateApprovalSchema(unittest.TestCase):

    def test_valid_fixture_no_errors(self):
        approval = _load_fixture_approval()
        errors = validate_approval_schema(approval)
        self.assertEqual(errors, [], f"エラーが発生: {errors}")

    def test_missing_required_key(self):
        approval = _load_fixture_approval()
        del approval["teaser_hash"]
        errors = validate_approval_schema(approval)
        self.assertTrue(len(errors) > 0)

    def test_extra_key_rejected(self):
        approval = _load_fixture_approval()
        approval["extra_field"] = "value"
        errors = validate_approval_schema(approval)
        self.assertTrue(any("不明" in e for e in errors), errors)

    def test_invalid_week_id(self):
        approval = _load_fixture_approval()
        approval["week_id"] = "2026-99"
        errors = validate_approval_schema(approval)
        self.assertTrue(any("week_id" in e for e in errors), errors)

    def test_revision_zero_rejected(self):
        approval = _load_fixture_approval()
        approval["revision"] = 0
        errors = validate_approval_schema(approval)
        self.assertTrue(any("revision" in e for e in errors), errors)

    def test_invalid_approved_at(self):
        approval = _load_fixture_approval()
        approval["approved_at"] = "not-a-date"
        errors = validate_approval_schema(approval)
        self.assertTrue(any("approved_at" in e for e in errors), errors)

    def test_invalid_draft_generated_at(self):
        approval = _load_fixture_approval()
        approval["draft_generated_at"] = "not-a-date"
        errors = validate_approval_schema(approval)
        self.assertTrue(any("draft_generated_at" in e for e in errors), errors)

    def test_approved_by_empty_rejected(self):
        approval = _load_fixture_approval()
        approval["approved_by"] = ""
        errors = validate_approval_schema(approval)
        self.assertTrue(any("approved_by" in e for e in errors), errors)

    def test_approved_by_too_long_rejected(self):
        approval = _load_fixture_approval()
        approval["approved_by"] = "a" * 65
        errors = validate_approval_schema(approval)
        self.assertTrue(any("approved_by" in e for e in errors), errors)

    def test_teaser_hash_uppercase_rejected(self):
        approval = _load_fixture_approval()
        approval["teaser_hash"] = "A" * 64
        errors = validate_approval_schema(approval)
        self.assertTrue(any("teaser_hash" in e for e in errors), errors)

    def test_teaser_hash_short_rejected(self):
        approval = _load_fixture_approval()
        approval["teaser_hash"] = "abc"
        errors = validate_approval_schema(approval)
        self.assertTrue(any("teaser_hash" in e for e in errors), errors)

    def test_approval_version_two_rejected(self):
        approval = _load_fixture_approval()
        approval["approval_version"] = 2
        errors = validate_approval_schema(approval)
        self.assertTrue(any("approval_version" in e for e in errors), errors)

    def test_z_suffix_datetime_ok(self):
        approval = _load_fixture_approval()
        approval["approved_at"] = "2026-06-29T01:00:00Z"
        errors = validate_approval_schema(approval)
        self.assertEqual(errors, [], f"Z suffix が拒否された: {errors}")


# ─── are_approvals_equal ─────────────────────────────────────────────────────

class TestAreApprovalsEqual(unittest.TestCase):

    def test_same_approval_equal(self):
        a = _load_fixture_approval()
        b = copy.deepcopy(a)
        self.assertTrue(are_approvals_equal(a, b))

    def test_different_approved_at_still_equal(self):
        a = _load_fixture_approval()
        b = copy.deepcopy(a)
        b["approved_at"] = "2026-06-30T00:00:00+00:00"
        self.assertTrue(are_approvals_equal(a, b))

    def test_different_week_id_not_equal(self):
        a = _load_fixture_approval()
        b = copy.deepcopy(a)
        b["week_id"] = "2026-W27"
        self.assertFalse(are_approvals_equal(a, b))

    def test_different_revision_not_equal(self):
        a = _load_fixture_approval()
        b = copy.deepcopy(a)
        b["revision"] = 2
        self.assertFalse(are_approvals_equal(a, b))

    def test_different_operator_not_equal(self):
        a = _load_fixture_approval()
        b = copy.deepcopy(a)
        b["approved_by"] = "another-operator"
        self.assertFalse(are_approvals_equal(a, b))

    def test_different_teaser_hash_not_equal(self):
        a = _load_fixture_approval()
        b = copy.deepcopy(a)
        b["teaser_hash"] = "b" * 64
        self.assertFalse(are_approvals_equal(a, b))

    def test_different_paid_body_hash_not_equal(self):
        a = _load_fixture_approval()
        b = copy.deepcopy(a)
        b["paid_body_hash"] = "c" * 64
        self.assertFalse(are_approvals_equal(a, b))

    def test_generated_at_utc_equivalence(self):
        """Z と +00:00 を同一とみなす。"""
        a = _load_fixture_approval()
        b = copy.deepcopy(a)
        b["draft_generated_at"] = a["draft_generated_at"].replace("+00:00", "Z")
        self.assertTrue(are_approvals_equal(a, b))

    def test_different_generated_at_not_equal(self):
        a = _load_fixture_approval()
        b = copy.deepcopy(a)
        b["draft_generated_at"] = "2026-06-28T00:00:00+00:00"
        self.assertFalse(are_approvals_equal(a, b))


# ─── parse_approval_input ────────────────────────────────────────────────────

class TestParseApprovalInput(unittest.TestCase):

    def test_exact_match(self):
        self.assertTrue(parse_approval_input("APPROVE 2026-W26", "2026-W26"))

    def test_with_leading_trailing_space(self):
        self.assertTrue(parse_approval_input("  APPROVE 2026-W26  ", "2026-W26"))

    def test_wrong_week_id(self):
        self.assertFalse(parse_approval_input("APPROVE 2026-W25", "2026-W26"))

    def test_lowercase_approve(self):
        self.assertFalse(parse_approval_input("approve 2026-W26", "2026-W26"))

    def test_empty_input(self):
        self.assertFalse(parse_approval_input("", "2026-W26"))

    def test_yes_rejected(self):
        self.assertFalse(parse_approval_input("yes", "2026-W26"))

    def test_approve_without_week_id(self):
        self.assertFalse(parse_approval_input("APPROVE", "2026-W26"))

    def test_different_week_format(self):
        self.assertTrue(parse_approval_input("APPROVE 2023-W01", "2023-W01"))


# ─── load_approval / save_approval ───────────────────────────────────────────

class TestLoadSaveApproval(unittest.TestCase):

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            approval = _load_fixture_approval()
            p = Path(tmpdir) / "approval.json"
            saved = save_approval(approval, path=p)
            self.assertEqual(saved, p)
            loaded = load_approval("2026-W26", path=p)
            self.assertEqual(loaded, approval)

    def test_load_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "nonexistent.json"
            result = load_approval("2026-W26", path=p)
            self.assertIsNone(result)

    def test_saved_file_permissions_600(self):
        import stat as stat_mod
        with tempfile.TemporaryDirectory() as tmpdir:
            approval = _load_fixture_approval()
            p = Path(tmpdir) / "approval.json"
            save_approval(approval, path=p)
            mode = p.stat().st_mode
            self.assertFalse(mode & stat_mod.S_IRGRP, "group read bit should not be set")
            self.assertFalse(mode & stat_mod.S_IWGRP, "group write bit should not be set")
            self.assertFalse(mode & stat_mod.S_IROTH, "other read bit should not be set")

    def test_saved_content_valid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            approval = _load_fixture_approval()
            p = Path(tmpdir) / "approval.json"
            save_approval(approval, path=p)
            raw = p.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            self.assertEqual(parsed["week_id"], approval["week_id"])

    def test_load_invalid_json_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "bad.json"
            p.write_text("not-json", encoding="utf-8")
            with self.assertRaises(ApprovalFileError):
                load_approval("2026-W26", path=p)

    def test_no_free_teaser_saved(self):
        """承認ファイルに free_teaser が保存されないこと。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            approval = _load_fixture_approval()
            p = Path(tmpdir) / "approval.json"
            save_approval(approval, path=p)
            raw = p.read_text(encoding="utf-8")
            self.assertNotIn("free_teaser", raw)

    def test_no_paid_body_saved(self):
        """承認ファイルに paid_body キー（本文）が保存されないこと。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            approval = _load_fixture_approval()
            p = Path(tmpdir) / "approval.json"
            save_approval(approval, path=p)
            raw = p.read_text(encoding="utf-8")
            # "paid_body_hash" は含まれるが "paid_body": は含まれないこと
            self.assertNotIn('"paid_body":', raw)


# ─── verify_db_draft_matches_local ───────────────────────────────────────────

class TestVerifyDbDraftMatchesLocal(unittest.TestCase):

    def setUp(self):
        self.draft = _load_fixture_draft()
        self.db_row = _make_db_row(self.draft)

    def test_valid_match_no_errors(self):
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertEqual(errors, [], f"エラーが発生: {errors}")

    def test_withdrawn_status_rejected(self):
        self.db_row["status"] = "withdrawn"
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertTrue(any("withdrawn" in e for e in errors), errors)

    def test_published_status_rejected(self):
        self.db_row["status"] = "published"
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertTrue(any("published" in e for e in errors), errors)

    def test_week_id_mismatch(self):
        self.db_row["week_id"] = "2026-W27"
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertTrue(any("week_id" in e for e in errors), errors)

    def test_revision_mismatch(self):
        self.db_row["revision"] = 2
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertTrue(any("revision" in e for e in errors), errors)

    def test_generated_at_mismatch(self):
        self.db_row["generated_at"] = "2026-06-28T00:00:00+00:00"
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertTrue(any("generated_at" in e for e in errors), errors)

    def test_generated_at_utc_equivalence(self):
        """Z と +00:00 を同一とみなす。"""
        self.db_row["generated_at"] = self.draft["generated_at"].replace("+00:00", "Z")
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertEqual(errors, [], f"UTC 等価エラー: {errors}")

    def test_free_teaser_mismatch(self):
        self.db_row["free_teaser"] = copy.deepcopy(self.draft["free_teaser"])
        self.db_row["free_teaser"]["env_label"] = "変更後"
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertTrue(any("free_teaser" in e for e in errors), errors)

    def test_paid_body_mismatch(self):
        self.db_row["paid_body"] = copy.deepcopy(self.draft["paid_body"])
        self.db_row["paid_body"]["summary"] = "変更後サマリー"
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertTrue(any("paid_body" in e for e in errors), errors)

    def test_nonnull_teaser_hash_rejected(self):
        self.db_row["teaser_hash"] = _VALID_HASH_64
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertTrue(any("teaser_hash" in e for e in errors), errors)

    def test_nonnull_paid_body_hash_rejected(self):
        self.db_row["paid_body_hash"] = _VALID_HASH2_64
        errors = verify_db_draft_matches_local(self.db_row, self.draft)
        self.assertTrue(any("paid_body_hash" in e for e in errors), errors)

    def test_tampered_local_hash_detected(self):
        draft2 = copy.deepcopy(self.draft)
        draft2["teaser_hash"] = "a" * 64
        errors = verify_db_draft_matches_local(self.db_row, draft2)
        self.assertTrue(len(errors) > 0, "hash 改変が検出されなかった")


if __name__ == "__main__":
    unittest.main(verbosity=2)
