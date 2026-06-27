"""
test_weekly_publish.py — weekly_report_publish.py の単体テスト（DB 接続不要）

テスト対象:
  - verify_pre_publish(): 公開前改変チェック
  - build_publish_payload(): published PATCH payload 構築
  - verify_published_report(): 公開後整合確認
  - are_published_idempotent(): 冪等性チェック
"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_report_publish import (
    verify_pre_publish,
    build_publish_payload,
    verify_published_report,
    are_published_idempotent,
)

FIXTURES = Path(__file__).parent / "fixtures" / "weekly"

_VALID_HASH = "fdc609afb24d11c1f0782bf5361d93c44bdccbc0e7cacdc469e714c8afbcecd5"
_VALID_HASH2 = "3af1820138cedd6348c459806a74024d2826f38bb9196a15c1735cedaa3de0d9"


def _load_draft() -> dict:
    with open(FIXTURES / "draft_2026_W26_valid.json", encoding="utf-8") as f:
        return json.load(f)


def _load_approval() -> dict:
    with open(FIXTURES / "approval_2026_W26_valid.json", encoding="utf-8") as f:
        return json.load(f)


def _make_db_draft_row(draft: dict) -> dict:
    """draft から DB draft 行を模擬する（hash 列は NULL）。"""
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


def _make_published_row(draft: dict, approval: dict, published_at: str) -> dict:
    """公開後の DB 行を模擬する。"""
    row = _make_db_draft_row(draft)
    row.update({
        "status":         "published",
        "reviewed_at":    approval["approved_at"],
        "reviewed_by":    approval["approved_by"],
        "published_at":   published_at,
        "teaser_hash":    approval["teaser_hash"],
        "paid_body_hash": approval["paid_body_hash"],
    })
    return row


# ─── verify_pre_publish テスト ────────────────────────────────────────────────

class TestVerifyPrePublish(unittest.TestCase):

    def setUp(self):
        self.draft    = _load_draft()
        self.approval = _load_approval()
        self.db_row   = _make_db_draft_row(self.draft)

    def test_valid_no_errors(self):
        errors = verify_pre_publish(self.approval, self.draft, self.db_row)
        self.assertEqual(errors, [], f"エラーが発生: {errors}")

    def test_withdrawn_db_rejected(self):
        self.db_row["status"] = "withdrawn"
        errors = verify_pre_publish(self.approval, self.draft, self.db_row)
        self.assertTrue(any("withdrawn" in e for e in errors), errors)

    def test_published_db_rejected(self):
        self.db_row["status"] = "published"
        errors = verify_pre_publish(self.approval, self.draft, self.db_row)
        self.assertTrue(len(errors) > 0, "published status が拒否されなかった")

    def test_draft_teaser_hash_tampered(self):
        draft2 = copy.deepcopy(self.draft)
        draft2["teaser_hash"] = "a" * 64
        errors = verify_pre_publish(self.approval, draft2, self.db_row)
        self.assertTrue(any("teaser_hash" in e for e in errors), errors)

    def test_draft_paid_body_hash_tampered(self):
        draft2 = copy.deepcopy(self.draft)
        draft2["paid_body_hash"] = "b" * 64
        errors = verify_pre_publish(self.approval, draft2, self.db_row)
        self.assertTrue(any("paid_body_hash" in e for e in errors), errors)

    def test_db_free_teaser_tampered(self):
        row2 = copy.deepcopy(self.db_row)
        row2["free_teaser"] = copy.deepcopy(self.draft["free_teaser"])
        row2["free_teaser"]["env_label"] = "改変後ラベル"
        errors = verify_pre_publish(self.approval, self.draft, row2)
        self.assertTrue(len(errors) > 0, "DB free_teaser 改変が検出されなかった")

    def test_db_paid_body_tampered(self):
        row2 = copy.deepcopy(self.db_row)
        row2["paid_body"] = copy.deepcopy(self.draft["paid_body"])
        row2["paid_body"]["summary"] = "改変後サマリー"
        errors = verify_pre_publish(self.approval, self.draft, row2)
        self.assertTrue(len(errors) > 0, "DB paid_body 改変が検出されなかった")

    def test_revision_mismatch_draft(self):
        draft2 = copy.deepcopy(self.draft)
        draft2["revision"] = 2
        errors = verify_pre_publish(self.approval, draft2, self.db_row)
        self.assertTrue(any("revision" in e for e in errors), errors)

    def test_revision_mismatch_db(self):
        row2 = copy.deepcopy(self.db_row)
        row2["revision"] = 2
        errors = verify_pre_publish(self.approval, self.draft, row2)
        self.assertTrue(any("revision" in e for e in errors), errors)

    def test_generated_at_mismatch_draft(self):
        draft2 = copy.deepcopy(self.draft)
        draft2["generated_at"] = "2026-06-28T00:00:00+00:00"
        errors = verify_pre_publish(self.approval, draft2, self.db_row)
        self.assertTrue(any("generated_at" in e for e in errors), errors)

    def test_generated_at_z_suffix_ok(self):
        row2 = copy.deepcopy(self.db_row)
        row2["generated_at"] = self.draft["generated_at"].replace("+00:00", "Z")
        errors = verify_pre_publish(self.approval, self.draft, row2)
        self.assertEqual(errors, [], f"Z suffix エラー: {errors}")


# ─── build_publish_payload テスト ─────────────────────────────────────────────

class TestBuildPublishPayload(unittest.TestCase):

    def setUp(self):
        self.approval     = _load_approval()
        self.published_at = "2026-06-29T02:00:00+00:00"
        self.payload      = build_publish_payload(self.approval, self.published_at)

    def test_status_is_published(self):
        self.assertEqual(self.payload["status"], "published")

    def test_reviewed_at_from_approval(self):
        self.assertEqual(self.payload["reviewed_at"], self.approval["approved_at"])

    def test_reviewed_by_from_approval(self):
        self.assertEqual(self.payload["reviewed_by"], self.approval["approved_by"])

    def test_published_at(self):
        self.assertEqual(self.payload["published_at"], self.published_at)

    def test_teaser_hash_from_approval(self):
        self.assertEqual(self.payload["teaser_hash"], self.approval["teaser_hash"])

    def test_paid_body_hash_from_approval(self):
        self.assertEqual(self.payload["paid_body_hash"], self.approval["paid_body_hash"])

    def test_withdrawn_at_null(self):
        self.assertIsNone(self.payload["withdrawn_at"])

    def test_withdrawal_reason_null(self):
        self.assertIsNone(self.payload["withdrawal_reason"])

    def test_no_week_id_in_payload(self):
        self.assertNotIn("week_id", self.payload)

    def test_no_free_teaser_in_payload(self):
        self.assertNotIn("free_teaser", self.payload)

    def test_no_paid_body_in_payload(self):
        self.assertNotIn("paid_body", self.payload)

    def test_no_revision_in_payload(self):
        self.assertNotIn("revision", self.payload)

    def test_no_generated_at_in_payload(self):
        self.assertNotIn("generated_at", self.payload)

    def test_exactly_eight_keys(self):
        self.assertEqual(len(self.payload), 8)


# ─── verify_published_report テスト ──────────────────────────────────────────

class TestVerifyPublishedReport(unittest.TestCase):

    def setUp(self):
        self.draft       = _load_draft()
        self.approval    = _load_approval()
        self.published_at = "2026-06-29T02:00:00+00:00"
        self.payload     = build_publish_payload(self.approval, self.published_at)
        self.db_row      = _make_published_row(self.draft, self.approval, self.published_at)

    def test_valid_no_mismatches(self):
        mismatches = verify_published_report(self.db_row, self.payload, self.approval)
        self.assertEqual(mismatches, [], f"不一致: {mismatches}")

    def test_wrong_status(self):
        row2 = copy.deepcopy(self.db_row)
        row2["status"] = "draft"
        mismatches = verify_published_report(row2, self.payload, self.approval)
        self.assertTrue(any("status" in m for m in mismatches), mismatches)

    def test_wrong_reviewed_at(self):
        row2 = copy.deepcopy(self.db_row)
        row2["reviewed_at"] = "2026-06-28T00:00:00+00:00"
        mismatches = verify_published_report(row2, self.payload, self.approval)
        self.assertTrue(any("reviewed_at" in m for m in mismatches), mismatches)

    def test_wrong_reviewed_by(self):
        row2 = copy.deepcopy(self.db_row)
        row2["reviewed_by"] = "wrong-operator"
        mismatches = verify_published_report(row2, self.payload, self.approval)
        self.assertTrue(any("reviewed_by" in m for m in mismatches), mismatches)

    def test_wrong_teaser_hash(self):
        row2 = copy.deepcopy(self.db_row)
        row2["teaser_hash"] = "a" * 64
        mismatches = verify_published_report(row2, self.payload, self.approval)
        self.assertTrue(any("teaser_hash" in m for m in mismatches), mismatches)

    def test_wrong_paid_body_hash(self):
        row2 = copy.deepcopy(self.db_row)
        row2["paid_body_hash"] = "b" * 64
        mismatches = verify_published_report(row2, self.payload, self.approval)
        self.assertTrue(any("paid_body_hash" in m for m in mismatches), mismatches)

    def test_non_null_withdrawn_at(self):
        row2 = copy.deepcopy(self.db_row)
        row2["withdrawn_at"] = "2026-06-30T00:00:00+00:00"
        mismatches = verify_published_report(row2, self.payload, self.approval)
        self.assertTrue(any("withdrawn_at" in m for m in mismatches), mismatches)

    def test_free_teaser_tampered_detected(self):
        row2 = copy.deepcopy(self.db_row)
        row2["free_teaser"] = copy.deepcopy(self.draft["free_teaser"])
        row2["free_teaser"]["env_label"] = "改変後"
        mismatches = verify_published_report(row2, self.payload, self.approval)
        self.assertTrue(len(mismatches) > 0, "free_teaser 改変が検出されなかった")

    def test_paid_body_tampered_detected(self):
        row2 = copy.deepcopy(self.db_row)
        row2["paid_body"] = copy.deepcopy(self.draft["paid_body"])
        row2["paid_body"]["summary"] = "改変後サマリー"
        mismatches = verify_published_report(row2, self.payload, self.approval)
        self.assertTrue(len(mismatches) > 0, "paid_body 改変が検出されなかった")

    def test_revision_changed_detected(self):
        row2 = copy.deepcopy(self.db_row)
        row2["revision"] = 2
        mismatches = verify_published_report(row2, self.payload, self.approval)
        self.assertTrue(any("revision" in m for m in mismatches), mismatches)

    def test_utc_equivalence_published_at(self):
        row2 = copy.deepcopy(self.db_row)
        row2["published_at"] = self.published_at.replace("+00:00", "Z")
        mismatches = verify_published_report(row2, self.payload, self.approval)
        self.assertEqual(mismatches, [], f"Z suffix エラー: {mismatches}")


# ─── are_published_idempotent テスト ─────────────────────────────────────────

class TestArePublishedIdempotent(unittest.TestCase):

    def setUp(self):
        self.draft       = _load_draft()
        self.approval    = _load_approval()
        self.published_at = "2026-06-29T02:00:00+00:00"
        self.payload     = build_publish_payload(self.approval, self.published_at)
        self.db_row      = _make_published_row(self.draft, self.approval, self.published_at)

    def test_same_content_idempotent(self):
        self.assertTrue(
            are_published_idempotent(self.db_row, self.payload, self.approval)
        )

    def test_different_published_at_still_idempotent(self):
        row2 = copy.deepcopy(self.db_row)
        row2["published_at"] = "2026-06-30T00:00:00+00:00"
        self.assertTrue(
            are_published_idempotent(row2, self.payload, self.approval)
        )

    def test_different_reviewed_at_not_idempotent(self):
        row2 = copy.deepcopy(self.db_row)
        row2["reviewed_at"] = "2026-06-28T00:00:00+00:00"
        self.assertFalse(
            are_published_idempotent(row2, self.payload, self.approval)
        )

    def test_different_reviewed_by_not_idempotent(self):
        row2 = copy.deepcopy(self.db_row)
        row2["reviewed_by"] = "other-operator"
        self.assertFalse(
            are_published_idempotent(row2, self.payload, self.approval)
        )

    def test_different_teaser_hash_not_idempotent(self):
        approval2 = copy.deepcopy(self.approval)
        approval2["teaser_hash"] = "a" * 64
        self.assertFalse(
            are_published_idempotent(self.db_row, self.payload, approval2)
        )

    def test_different_revision_not_idempotent(self):
        row2 = copy.deepcopy(self.db_row)
        row2["revision"] = 2
        self.assertFalse(
            are_published_idempotent(row2, self.payload, self.approval)
        )

    def test_reviewed_at_utc_equivalence(self):
        row2 = copy.deepcopy(self.db_row)
        row2["reviewed_at"] = self.approval["approved_at"].replace("+00:00", "Z")
        self.assertTrue(
            are_published_idempotent(row2, self.payload, self.approval)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
