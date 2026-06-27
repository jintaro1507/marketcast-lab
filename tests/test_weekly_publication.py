"""
test_weekly_publication.py — 公開ワークフローの統合単体テスト（DB 接続・HTTP 不要）

テスト対象:
  - prepare → pages_verified → db_published → completed の段階的遷移
  - pub state とファイルのリンク整合
  - draft アーカイブ
  - prepare/finalize CLI の入力パース
  - pub state 冪等性チェック
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_report_builder import compute_hash
from weekly_pages import (
    PUBLICATION_VERSION,
    SCHEMA_VERSION,
    advance_stage,
    archive_draft,
    build_index_entry,
    build_initial_pub_state,
    build_public_teaser,
    check_forbidden_keys_deep,
    load_pub_state,
    load_public_index,
    save_pub_state,
    update_index,
    validate_public_index,
    validate_public_teaser,
    write_public_index,
    write_public_teaser,
    PubStateError,
    IndexUpdateError,
)

# ─── テストデータ ──────────────────────────────────────────────────────────────

def _make_free_teaser(week_id: str = "2022-W01") -> dict:
    return {
        "week_id":           week_id,
        "title":             f"Weekly Marketcast テスト（{week_id}）",
        "period_start":      "2022-01-03",
        "period_end":        "2022-01-07",
        "env_label":         "テスト環境ラベル",
        "teaser_summary":    "テストサマリー。",
        "featured_theme":    None,
        "top_match_preview": None,
        "disclaimer":        "テスト免責事項。",
    }


def _make_draft(week_id: str = "2022-W01") -> dict:
    ft = _make_free_teaser(week_id)
    return {
        "week_id":        week_id,
        "revision":       1,
        "generated_at":   "2022-01-09T00:00:00+00:00",
        "teaser_hash":    compute_hash(ft),
        "paid_body_hash": "b" * 64,
        "free_teaser":    ft,
        "paid_body":      {"summary": "paid content"},
        "warnings":       [],
        "hard_errors":    [],
    }


def _make_approval(week_id: str = "2022-W01") -> dict:
    ft = _make_free_teaser(week_id)
    return {
        "week_id":            week_id,
        "revision":           1,
        "approved_at":        "2022-01-09T01:00:00+00:00",
        "approved_by":        "test-operator",
        "draft_generated_at": "2022-01-09T00:00:00+00:00",
        "teaser_hash":        compute_hash(ft),
        "paid_body_hash":     "b" * 64,
        "approval_version":   1,
    }


# ─── CLI 入力パース ────────────────────────────────────────────────────────────

class TestParsePrepareInput(unittest.TestCase):

    def _parse(self, user_input, week_id):
        return user_input.strip() == f"PREPARE {week_id}"

    def test_exact_match(self):
        self.assertTrue(self._parse("PREPARE 2022-W01", "2022-W01"))

    def test_trailing_space_stripped(self):
        self.assertTrue(self._parse("PREPARE 2022-W01  ", "2022-W01"))

    def test_wrong_week_id(self):
        self.assertFalse(self._parse("PREPARE 2023-W01", "2022-W01"))

    def test_lowercase_rejected(self):
        self.assertFalse(self._parse("prepare 2022-W01", "2022-W01"))

    def test_empty_input_rejected(self):
        self.assertFalse(self._parse("", "2022-W01"))

    def test_approve_rejected(self):
        self.assertFalse(self._parse("APPROVE 2022-W01", "2022-W01"))

    def test_yes_rejected(self):
        self.assertFalse(self._parse("yes", "2022-W01"))


class TestParsePublishInput(unittest.TestCase):

    def _parse(self, user_input, week_id):
        return user_input.strip() == f"PUBLISH {week_id}"

    def test_exact_match(self):
        self.assertTrue(self._parse("PUBLISH 2022-W01", "2022-W01"))

    def test_wrong_week_id(self):
        self.assertFalse(self._parse("PUBLISH 2023-W01", "2022-W01"))

    def test_prepare_rejected(self):
        self.assertFalse(self._parse("PREPARE 2022-W01", "2022-W01"))

    def test_lowercase_rejected(self):
        self.assertFalse(self._parse("publish 2022-W01", "2022-W01"))

    def test_empty_input_rejected(self):
        self.assertFalse(self._parse("", "2022-W01"))


# ─── Prepare フロー ────────────────────────────────────────────────────────────

class TestPrepareWorkflow(unittest.TestCase):
    """prepare の主要ロジックをユニットテスト（DB・HTTP なし）。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.pub_dir = self.tmpdir / "publications"
        self.data_dir = self.tmpdir / "data" / "weekly"
        self.data_dir.mkdir(parents=True)
        self.draft = _make_draft()
        self.approval = _make_approval()
        self.published_at = "2022-01-10T01:00:00+00:00"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_build_pub_state_and_save(self):
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        path = self.pub_dir / "2022-W01_publication.json"
        save_pub_state(pub_state, path=path)
        loaded = load_pub_state("2022-W01", path=path)
        self.assertEqual(loaded["stage"], "prepared")

    def test_teaser_built_correctly(self):
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        self.assertEqual(teaser["week_id"], "2022-W01")
        self.assertEqual(teaser["teaser_hash"], self.approval["teaser_hash"])
        self.assertNotIn("paid_body", teaser)

    def test_teaser_validates_ok(self):
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        errors = validate_public_teaser(teaser)
        self.assertEqual(errors, [], f"検証エラー: {errors}")

    def test_teaser_no_forbidden_keys(self):
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        errors = check_forbidden_keys_deep(teaser)
        self.assertEqual(errors, [], f"禁止キー: {errors}")

    def test_index_created_with_entry(self):
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        entry = build_index_entry(teaser)
        new_index, action = update_index(None, entry)
        self.assertEqual(action, "added")
        self.assertEqual(new_index["latest_week_id"], "2022-W01")

    def test_index_validates_ok(self):
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        entry = build_index_entry(teaser)
        new_index, _ = update_index(None, entry)
        errors = validate_public_index(new_index)
        self.assertEqual(errors, [], f"インデックス検証エラー: {errors}")

    def test_teaser_file_written(self):
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        path = self.data_dir / "2022-W01.json"
        result = write_public_teaser(teaser, path)
        self.assertEqual(result, "written")
        self.assertTrue(path.exists())

    def test_index_file_written(self):
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        entry = build_index_entry(teaser)
        new_index, _ = update_index(None, entry)
        path = self.data_dir / "index.json"
        write_public_index(new_index, path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(loaded["reports"]), 1)

    def test_prepare_idempotent_same_content(self):
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        path = self.data_dir / "2022-W01.json"
        write_public_teaser(teaser, path)
        result = write_public_teaser(teaser, path)
        self.assertEqual(result, "unchanged")

    def test_published_at_from_pub_state(self):
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        self.assertEqual(teaser["published_at"], self.published_at)


# ─── Verify フロー ────────────────────────────────────────────────────────────

class TestVerifyWorkflow(unittest.TestCase):
    """pages_verified ステージへの遷移をユニットテスト。"""

    def test_advance_to_pages_verified(self):
        state = {
            "publication_version": PUBLICATION_VERSION,
            "week_id": "2022-W01",
            "revision": 1,
            "teaser_hash": "a" * 64,
            "paid_body_hash": "b" * 64,
            "published_at": "2022-01-10T01:00:00+00:00",
            "stage": "prepared",
            "prepared_at": "2022-01-10T01:00:00+00:00",
        }
        new_state = advance_stage(state, "pages_verified")
        self.assertEqual(new_state["stage"], "pages_verified")
        self.assertIn("pages_verified_at", new_state)

    def test_cannot_skip_verify(self):
        state = {
            "stage": "prepared",
            "publication_version": PUBLICATION_VERSION,
            "week_id": "2022-W01",
        }
        with self.assertRaises(PubStateError):
            advance_stage(state, "db_published")


# ─── Finalize フロー ──────────────────────────────────────────────────────────

class TestFinalizeWorkflow(unittest.TestCase):
    """DB published 化後の段階的遷移をユニットテスト。"""

    def test_advance_to_db_published(self):
        state = {
            "publication_version": PUBLICATION_VERSION,
            "week_id": "2022-W01",
            "revision": 1,
            "teaser_hash": "a" * 64,
            "paid_body_hash": "b" * 64,
            "published_at": "2022-01-10T01:00:00+00:00",
            "stage": "pages_verified",
            "prepared_at": "2022-01-10T01:00:00+00:00",
            "pages_verified_at": "2022-01-10T02:00:00+00:00",
        }
        new_state = advance_stage(state, "db_published")
        self.assertEqual(new_state["stage"], "db_published")
        self.assertIn("db_published_at", new_state)

    def test_advance_to_completed(self):
        state = {
            "publication_version": PUBLICATION_VERSION,
            "week_id": "2022-W01",
            "revision": 1,
            "teaser_hash": "a" * 64,
            "paid_body_hash": "b" * 64,
            "published_at": "2022-01-10T01:00:00+00:00",
            "stage": "db_published",
            "prepared_at": "2022-01-10T01:00:00+00:00",
            "pages_verified_at": "2022-01-10T02:00:00+00:00",
            "db_published_at": "2022-01-10T03:00:00+00:00",
        }
        new_state = advance_stage(state, "completed")
        self.assertEqual(new_state["stage"], "completed")
        self.assertIn("completed_at", new_state)

    def test_cannot_finalize_from_prepared(self):
        state = {"stage": "prepared", "publication_version": PUBLICATION_VERSION, "week_id": "2022-W01"}
        with self.assertRaises(PubStateError):
            advance_stage(state, "db_published")


# ─── 全体フロー ────────────────────────────────────────────────────────────────

class TestFullWorkflowPipeline(unittest.TestCase):
    """prepare → pages_verified → db_published → completed の全遷移を結合テスト。"""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.pub_dir = self.tmpdir / "publications"
        self.data_dir = self.tmpdir / "data" / "weekly"
        self.archive_dir = self.tmpdir / "archives"
        self.draft_dir = self.tmpdir / "drafts"
        self.draft_dir.mkdir(parents=True)
        self.data_dir.mkdir(parents=True)

        self.draft = _make_draft()
        self.approval = _make_approval()
        self.published_at = "2022-01-10T01:00:00+00:00"

        # draft ファイルを書き込む
        self.draft_path = self.draft_dir / "2022-W01_draft.json"
        self.draft_path.write_text(
            json.dumps(self.draft, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_pipeline(self):
        week_id = "2022-W01"
        pub_path = self.pub_dir / f"{week_id}_publication.json"

        # 1. prepare
        pub_state = build_initial_pub_state(week_id, self.approval, self.published_at)
        save_pub_state(pub_state, path=pub_path)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        entry = build_index_entry(teaser)
        new_index, _ = update_index(None, entry)
        teaser_path = self.data_dir / f"{week_id}.json"
        index_path = self.data_dir / "index.json"
        write_public_teaser(teaser, teaser_path)
        write_public_index(new_index, index_path)

        loaded_state = load_pub_state(week_id, path=pub_path)
        self.assertEqual(loaded_state["stage"], "prepared")

        # 2. pages_verified
        state2 = advance_stage(loaded_state, "pages_verified")
        save_pub_state(state2, path=pub_path)
        loaded_state2 = load_pub_state(week_id, path=pub_path)
        self.assertEqual(loaded_state2["stage"], "pages_verified")

        # 3. db_published
        state3 = advance_stage(loaded_state2, "db_published")
        save_pub_state(state3, path=pub_path)
        loaded_state3 = load_pub_state(week_id, path=pub_path)
        self.assertEqual(loaded_state3["stage"], "db_published")

        # 4. archive
        import weekly_pages
        orig = weekly_pages.ARCHIVES_DIR
        weekly_pages.ARCHIVES_DIR = self.archive_dir
        try:
            archive_path = archive_draft(self.draft_path, week_id)
        finally:
            weekly_pages.ARCHIVES_DIR = orig
        self.assertTrue(archive_path.exists())

        # 5. completed
        state4 = advance_stage(loaded_state3, "completed")
        save_pub_state(state4, path=pub_path)
        loaded_state4 = load_pub_state(week_id, path=pub_path)
        self.assertEqual(loaded_state4["stage"], "completed")

    def test_pub_state_preserves_published_at(self):
        """pub_state の published_at はすべてのステージで保存される。"""
        week_id = "2022-W01"
        pub_path = self.pub_dir / f"{week_id}_publication.json"
        pub_state = build_initial_pub_state(week_id, self.approval, self.published_at)
        save_pub_state(pub_state, path=pub_path)

        state = load_pub_state(week_id, path=pub_path)
        state2 = advance_stage(state, "pages_verified")
        save_pub_state(state2, path=pub_path)
        state3 = load_pub_state(week_id, path=pub_path)
        self.assertEqual(state3["published_at"], self.published_at)

    def test_teaser_hash_consistency(self):
        """teaser_hash が pub_state / teaser / approval で一致する。"""
        pub_state = build_initial_pub_state("2022-W01", self.approval, self.published_at)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        self.assertEqual(pub_state["teaser_hash"], self.approval["teaser_hash"])
        self.assertEqual(teaser["teaser_hash"], self.approval["teaser_hash"])

    def test_multiple_weeks_index(self):
        """複数週のエントリをインデックスに追加できる。"""
        e1 = build_index_entry(_make_teaser_from_draft(_make_draft("2022-W01"), _make_approval("2022-W01"), "2022-01-10T01:00:00+00:00"))
        e2_hash = "c" * 64
        e2 = {
            "week_id": "2023-W01", "revision": 1,
            "published_at": "2023-01-10T01:00:00+00:00",
            "title": "テスト2", "period_start": "2023-01-02",
            "period_end": "2023-01-06", "env_label": "テスト",
            "teaser_hash": e2_hash,
        }
        idx, _ = update_index(None, e1)
        idx, _ = update_index(idx, e2)
        self.assertEqual(len(idx["reports"]), 2)
        self.assertEqual(idx["latest_week_id"], "2023-W01")


def _make_teaser_from_draft(draft: dict, approval: dict, published_at: str) -> dict:
    from weekly_pages import build_initial_pub_state, build_public_teaser
    ps = build_initial_pub_state(draft["week_id"], approval, published_at)
    return build_public_teaser(draft, approval, ps)


def _make_teaser(week_id: str = "2022-W01") -> dict:
    ft = _make_free_teaser(week_id)
    from weekly_pages import SCHEMA_VERSION
    return {
        "schema_version": SCHEMA_VERSION,
        "week_id":        week_id,
        "revision":       1,
        "published_at":   "2022-01-10T01:00:00+00:00",
        "teaser_hash":    compute_hash(ft),
        "free_teaser":    ft,
    }


# ─── 公開後 hash 照合 ─────────────────────────────────────────────────────────

class TestHashConsistency(unittest.TestCase):
    """Pages teaser_hash と DB teaser_hash の照合ロジックをテスト。"""

    def _make_db_published_row(self, teaser_hash: str) -> dict:
        return {
            "status":         "published",
            "teaser_hash":    teaser_hash,
            "paid_body_hash": "b" * 64,
            "reviewed_at":    "2022-01-09T01:00:00+00:00",
            "reviewed_by":    "test-operator",
            "published_at":   "2022-01-10T01:00:00+00:00",
            "withdrawn_at":   None,
            "withdrawal_reason": None,
            "revision":       1,
        }

    def test_matching_hashes(self):
        ft = _make_free_teaser()
        h = compute_hash(ft)
        teaser = _make_teaser()
        db_row = self._make_db_published_row(h)
        self.assertEqual(teaser["teaser_hash"], db_row["teaser_hash"])

    def test_mismatching_hashes_detected(self):
        teaser = _make_teaser()
        db_row = self._make_db_published_row("different" + "a" * 55)
        self.assertNotEqual(teaser["teaser_hash"], db_row["teaser_hash"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
