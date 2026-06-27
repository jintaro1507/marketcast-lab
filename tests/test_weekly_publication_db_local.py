"""
test_weekly_publication_db_local.py — 公開ワークフローのローカル Supabase 統合テスト

実行条件:
  - ローカル Supabase が起動していること
  - または SUPABASE_TEST_URL / SUPABASE_TEST_SERVICE_KEY 環境変数が設定されていること

本番 Supabase URL が検出された場合はスキップする。

実行方法:
  python -m pytest tests/test_weekly_publication_db_local.py -v
"""
import copy
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_config import PROD_PROJECT_REF
from weekly_dates import week_id_to_period
from weekly_db import WeeklyDBError
from weekly_report_builder import compute_hash
from weekly_report_db import WeeklyReportDB, build_db_payload, validate_draft
from weekly_approval import build_approval_payload, validate_approval_schema
from weekly_report_publish import (
    verify_pre_publish,
    build_publish_payload,
    verify_published_report,
    apply_published_transition,
)
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
)

# ─── 接続設定 ─────────────────────────────────────────────────────────────────

_LOCAL_URL    = "http://127.0.0.1:54321"
_LOCAL_SVC_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0"
    ".EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)

TEST_URL = os.environ.get("SUPABASE_TEST_URL", _LOCAL_URL)
TEST_KEY = os.environ.get("SUPABASE_TEST_SERVICE_KEY", _LOCAL_SVC_KEY)

# W2-3: 2024-W01 / W2-4A: 2023-W01 と衝突しない過去の週
TEST_WEEK_ID = "2022-W01"

FIXTURES = Path(__file__).parent / "fixtures" / "weekly"

_APPROVED_AT  = "2022-01-09T01:00:00+00:00"
_PUBLISHED_AT = "2022-01-10T01:00:00+00:00"


def _load_draft() -> dict:
    with open(FIXTURES / "draft_2026_W26_valid.json", encoding="utf-8") as f:
        d = json.load(f)
    ps, pe = week_id_to_period(TEST_WEEK_ID)
    d["week_id"] = TEST_WEEK_ID
    d["free_teaser"]["week_id"]      = TEST_WEEK_ID
    d["free_teaser"]["period_start"] = str(ps)
    d["free_teaser"]["period_end"]   = str(pe)
    d["free_teaser"]["title"]        = f"Weekly Marketcast テスト（{TEST_WEEK_ID}）"
    d["teaser_hash"]    = compute_hash(d["free_teaser"])
    d["paid_body_hash"] = compute_hash(d["paid_body"])
    return d


def _build_approval(draft: dict) -> dict:
    return build_approval_payload(TEST_WEEK_ID, draft, "test-operator", _APPROVED_AT)


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


# ─── 公開準備（ファイル生成）─────────────────────────────────────────────────

@SKIP_NO_DB
class TestPreparePublication(unittest.TestCase):
    """pub_state 構築・teaser/index ファイル生成の統合テスト。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(cls)
        cls.db = WeeklyReportDB(TEST_URL, TEST_KEY)

    def setUp(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass
        self.tmpdir = Path(tempfile.mkdtemp())
        self.pub_dir  = self.tmpdir / "publications"
        self.data_dir = self.tmpdir / "data" / "weekly"
        self.data_dir.mkdir(parents=True)

    def tearDown(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_draft(self) -> tuple[dict, dict]:
        draft = _load_draft()
        payload = build_db_payload(draft)
        self.db.insert_report_draft(payload)
        db_row = self.db.get_report_full(TEST_WEEK_ID)
        return draft, db_row

    def test_draft_matches_db_before_prepare(self):
        draft, db_row = self._insert_draft()
        approval = _build_approval(draft)
        errors = verify_pre_publish(approval, draft, db_row)
        self.assertEqual(errors, [], f"公開前検証エラー: {errors}")

    def test_pub_state_built_from_draft(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        pub_state = build_initial_pub_state(TEST_WEEK_ID, approval, _PUBLISHED_AT)
        self.assertEqual(pub_state["teaser_hash"], approval["teaser_hash"])
        self.assertEqual(pub_state["stage"], "prepared")

    def test_teaser_built_without_paid_body(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        pub_state = build_initial_pub_state(TEST_WEEK_ID, approval, _PUBLISHED_AT)
        teaser = build_public_teaser(draft, approval, pub_state)
        self.assertNotIn("paid_body", teaser)
        self.assertNotIn("paid_body_hash", teaser)

    def test_teaser_validates_ok(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        pub_state = build_initial_pub_state(TEST_WEEK_ID, approval, _PUBLISHED_AT)
        teaser = build_public_teaser(draft, approval, pub_state)
        errors = validate_public_teaser(teaser)
        self.assertEqual(errors, [], f"テスト teaser 検証エラー: {errors}")

    def test_no_forbidden_keys_in_teaser(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        pub_state = build_initial_pub_state(TEST_WEEK_ID, approval, _PUBLISHED_AT)
        teaser = build_public_teaser(draft, approval, pub_state)
        errors = check_forbidden_keys_deep(teaser)
        self.assertEqual(errors, [], f"禁止キー: {errors}")

    def test_teaser_hash_matches_free_teaser(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        pub_state = build_initial_pub_state(TEST_WEEK_ID, approval, _PUBLISHED_AT)
        teaser = build_public_teaser(draft, approval, pub_state)
        computed = compute_hash(teaser["free_teaser"])
        self.assertEqual(computed, teaser["teaser_hash"])

    def test_teaser_file_written(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        pub_state = build_initial_pub_state(TEST_WEEK_ID, approval, _PUBLISHED_AT)
        teaser = build_public_teaser(draft, approval, pub_state)
        path = self.data_dir / f"{TEST_WEEK_ID}.json"
        result = write_public_teaser(teaser, path)
        self.assertEqual(result, "written")
        self.assertTrue(path.exists())

    def test_index_updated_with_entry(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        pub_state = build_initial_pub_state(TEST_WEEK_ID, approval, _PUBLISHED_AT)
        teaser = build_public_teaser(draft, approval, pub_state)
        entry = build_index_entry(teaser)
        new_index, action = update_index(None, entry)
        self.assertEqual(action, "added")
        self.assertEqual(new_index["latest_week_id"], TEST_WEEK_ID)

    def test_index_validates_ok(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        pub_state = build_initial_pub_state(TEST_WEEK_ID, approval, _PUBLISHED_AT)
        teaser = build_public_teaser(draft, approval, pub_state)
        entry = build_index_entry(teaser)
        new_index, _ = update_index(None, entry)
        errors = validate_public_index(new_index)
        self.assertEqual(errors, [], f"インデックス検証エラー: {errors}")

    def test_pub_state_saved_and_loaded(self):
        draft, _ = self._insert_draft()
        approval = _build_approval(draft)
        pub_state = build_initial_pub_state(TEST_WEEK_ID, approval, _PUBLISHED_AT)
        path = self.pub_dir / f"{TEST_WEEK_ID}_publication.json"
        save_pub_state(pub_state, path=path)
        loaded = load_pub_state(TEST_WEEK_ID, path=path)
        self.assertEqual(loaded["stage"], "prepared")
        self.assertEqual(loaded["published_at"], _PUBLISHED_AT)


# ─── 公開完了（DB published 化）────────────────────────────────────────────────

@SKIP_NO_DB
class TestFinalizePublication(unittest.TestCase):
    """pub state pages_verified 状態から DB published 化の統合テスト。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(cls)
        cls.db = WeeklyReportDB(TEST_URL, TEST_KEY)

    def setUp(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass
        self.tmpdir = Path(tempfile.mkdtemp())
        self.pub_dir    = self.tmpdir / "publications"
        self.archive_dir = self.tmpdir / "archives"
        self.data_dir   = self.tmpdir / "data" / "weekly"
        self.draft_dir  = self.tmpdir / "drafts"
        self.data_dir.mkdir(parents=True)
        self.draft_dir.mkdir(parents=True)

        self.draft    = _load_draft()
        self.approval = _build_approval(self.draft)

        # draft ファイル書き込み
        self.draft_path = self.draft_dir / f"{TEST_WEEK_ID}_draft.json"
        self.draft_path.write_text(
            json.dumps(self.draft, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # DB に draft を挿入
        payload = build_db_payload(self.draft)
        self.db.insert_report_draft(payload)

        # pub state を pages_verified にセット
        pub_state = build_initial_pub_state(TEST_WEEK_ID, self.approval, _PUBLISHED_AT)
        state_pv = advance_stage(pub_state, "pages_verified")
        self.pub_path = self.pub_dir / f"{TEST_WEEK_ID}_publication.json"
        save_pub_state(state_pv, path=self.pub_path)

    def tearDown(self):
        try:
            self.db.delete_report(TEST_WEEK_ID)
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _finalize(self) -> dict:
        """DB を published に遷移させる（pub_state の published_at を使用）。"""
        pub_state = load_pub_state(TEST_WEEK_ID, path=self.pub_path)
        published_at = pub_state["published_at"]
        publish_payload = build_publish_payload(self.approval, published_at)
        revision = self.approval["revision"]
        rows = self.db.patch_report_to_published(
            TEST_WEEK_ID, revision, publish_payload
        )
        self.assertEqual(len(rows), 1, "PATCH で 1 件更新されるはず")
        return rows[0]

    def test_publish_at_from_pub_state_matches_db(self):
        db_row = self._finalize()
        from weekly_report_db import _datetimes_equal
        self.assertTrue(_datetimes_equal(db_row["published_at"], _PUBLISHED_AT))

    def test_db_status_published(self):
        db_row = self._finalize()
        self.assertEqual(db_row["status"], "published")

    def test_db_teaser_hash_matches(self):
        db_row = self._finalize()
        self.assertEqual(db_row["teaser_hash"], self.approval["teaser_hash"])

    def test_db_paid_body_hash_matches(self):
        db_row = self._finalize()
        self.assertEqual(db_row["paid_body_hash"], self.approval["paid_body_hash"])

    def test_verify_published_report_passes(self):
        db_row = self._finalize()
        payload = build_publish_payload(self.approval, db_row["published_at"])
        mismatches = verify_published_report(db_row, payload, self.approval)
        self.assertEqual(mismatches, [], f"不一致: {mismatches}")

    def test_pages_teaser_hash_matches_db(self):
        """Pages teaser の teaser_hash と DB の teaser_hash が一致する。"""
        pub_state = load_pub_state(TEST_WEEK_ID, path=self.pub_path)
        teaser = build_public_teaser(self.draft, self.approval, pub_state)
        db_row = self._finalize()
        self.assertEqual(teaser["teaser_hash"], db_row["teaser_hash"])

    def test_advance_to_db_published(self):
        self._finalize()
        pub_state = load_pub_state(TEST_WEEK_ID, path=self.pub_path)
        state_dbp = advance_stage(pub_state, "db_published")
        save_pub_state(state_dbp, path=self.pub_path)
        loaded = load_pub_state(TEST_WEEK_ID, path=self.pub_path)
        self.assertEqual(loaded["stage"], "db_published")


# ─── アーカイブ統合テスト ─────────────────────────────────────────────────────

@SKIP_NO_DB
class TestArchiveIntegration(unittest.TestCase):
    """draft アーカイブの統合テスト。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(cls)

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.archive_dir = self.tmpdir / "archives"
        self.draft_dir   = self.tmpdir / "drafts"
        self.draft_dir.mkdir(parents=True)
        self.draft = _load_draft()
        self.draft_path = self.draft_dir / f"{TEST_WEEK_ID}_draft.json"
        self.draft_path.write_text(
            json.dumps(self.draft, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _archive(self):
        import weekly_pages
        orig = weekly_pages.ARCHIVES_DIR
        weekly_pages.ARCHIVES_DIR = self.archive_dir
        try:
            return archive_draft(self.draft_path, TEST_WEEK_ID)
        finally:
            weekly_pages.ARCHIVES_DIR = orig

    def test_archive_file_created(self):
        path = self._archive()
        self.assertTrue(path.exists())

    def test_archive_content_matches_draft(self):
        path = self._archive()
        content = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(content["week_id"], TEST_WEEK_ID)

    def test_archive_dir_permissions(self):
        self._archive()
        mode = stat.S_IMODE(self.archive_dir.stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_archive_file_permissions(self):
        path = self._archive()
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_archive_idempotent(self):
        self._archive()
        path = self._archive()
        self.assertTrue(path.exists())

    def test_archive_does_not_modify_draft(self):
        self._archive()
        original = json.loads(self.draft_path.read_text(encoding="utf-8"))
        self.assertEqual(original["week_id"], TEST_WEEK_ID)


# ─── 本番ガード確認 ───────────────────────────────────────────────────────────

@SKIP_NO_DB
class TestPublicationProductionGuard(unittest.TestCase):
    """patch_report_to_published が本番 URL を拒否すること。"""

    def test_production_url_rejected(self):
        from weekly_db import ProductionGuardError
        draft = _load_draft()
        approval = _build_approval(draft)
        pub_payload = build_publish_payload(approval, _PUBLISHED_AT)
        prod_url = f"https://{PROD_PROJECT_REF}.supabase.co"
        db = WeeklyReportDB(prod_url, "dummy-key")
        with self.assertRaises(ProductionGuardError):
            db.patch_report_to_published(
                TEST_WEEK_ID, approval["revision"], pub_payload,
                allow_production=False,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
