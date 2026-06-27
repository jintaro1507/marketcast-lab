"""
test_weekly_pages.py — weekly_pages.py の単体テスト（DB 接続・HTTP 不要）

テスト対象:
  - pub state 管理（build / load / save / advance_stage）
  - 公開 JSON 構築（build_public_teaser / validate_public_teaser）
  - 禁止キー検査（check_forbidden_keys_deep）
  - index 管理（build_index_entry / update_index / validate_public_index）
  - ファイル書き込み（write_public_teaser / write_public_index）
  - アーカイブ（archive_draft）
  - HTTP 検証（verify_deployed_teaser / verify_deployed_index）—— HTTP はモック
"""
import copy
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_report_builder import compute_hash
from weekly_pages import (
    PUBLICATION_VERSION,
    SCHEMA_VERSION,
    IndexUpdateError,
    PubStateError,
    PublicFileError,
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
    verify_deployed_index,
    verify_deployed_teaser,
    write_public_index,
    write_public_teaser,
    _week_id_key,
)

# ─── テストデータ ──────────────────────────────────────────────────────────────

def _make_free_teaser(week_id: str = "2022-W01") -> dict:
    return {
        "week_id":          week_id,
        "title":            f"Weekly Marketcast テスト（{week_id}）",
        "period_start":     "2022-01-03",
        "period_end":       "2022-01-07",
        "env_label":        "テスト環境ラベル",
        "teaser_summary":   "テストサマリー。",
        "featured_theme":   None,
        "top_match_preview": None,
        "disclaimer":       "テスト免責事項。",
    }


def _make_teaser(week_id: str = "2022-W01") -> dict:
    ft = _make_free_teaser(week_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "week_id":        week_id,
        "revision":       1,
        "published_at":   "2022-01-10T01:00:00+00:00",
        "teaser_hash":    compute_hash(ft),
        "free_teaser":    ft,
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
        "week_id":             week_id,
        "revision":            1,
        "approved_at":         "2022-01-09T01:00:00+00:00",
        "approved_by":         "test-operator",
        "draft_generated_at":  "2022-01-09T00:00:00+00:00",
        "teaser_hash":         compute_hash(ft),
        "paid_body_hash":      "b" * 64,
        "approval_version":    1,
    }


def _make_pub_state(week_id: str = "2022-W01", stage: str = "prepared") -> dict:
    ft = _make_free_teaser(week_id)
    state = {
        "publication_version": PUBLICATION_VERSION,
        "week_id":      week_id,
        "revision":     1,
        "teaser_hash":  compute_hash(ft),
        "paid_body_hash": "b" * 64,
        "published_at": "2022-01-10T01:00:00+00:00",
        "stage":        stage,
        "prepared_at":  "2022-01-10T01:00:00+00:00",
    }
    if stage in ("pages_verified", "db_published", "completed"):
        state["pages_verified_at"] = "2022-01-10T02:00:00+00:00"
    if stage in ("db_published", "completed"):
        state["db_published_at"] = "2022-01-10T03:00:00+00:00"
    if stage == "completed":
        state["completed_at"] = "2022-01-10T04:00:00+00:00"
    return state


# ─── _week_id_key ────────────────────────────────────────────────────────────

class TestWeekIdKey(unittest.TestCase):

    def test_basic(self):
        self.assertEqual(_week_id_key("2022-W01"), (2022, 1))

    def test_double_digit_week(self):
        self.assertEqual(_week_id_key("2026-W26"), (2026, 26))

    def test_max_week(self):
        self.assertEqual(_week_id_key("2020-W53"), (2020, 53))

    def test_ordering_same_year(self):
        self.assertLess(_week_id_key("2022-W09"), _week_id_key("2022-W10"))

    def test_ordering_different_year(self):
        self.assertLess(_week_id_key("2022-W52"), _week_id_key("2023-W01"))

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            _week_id_key("not-a-week")


# ─── PubState 管理 ────────────────────────────────────────────────────────────

class TestBuildInitialPubState(unittest.TestCase):

    def setUp(self):
        self.approval = _make_approval()
        self.published_at = "2022-01-10T01:00:00+00:00"
        self.state = build_initial_pub_state("2022-W01", self.approval, self.published_at)

    def test_stage_is_prepared(self):
        self.assertEqual(self.state["stage"], "prepared")

    def test_week_id(self):
        self.assertEqual(self.state["week_id"], "2022-W01")

    def test_revision_from_approval(self):
        self.assertEqual(self.state["revision"], self.approval["revision"])

    def test_teaser_hash_from_approval(self):
        self.assertEqual(self.state["teaser_hash"], self.approval["teaser_hash"])

    def test_paid_body_hash_from_approval(self):
        self.assertEqual(self.state["paid_body_hash"], self.approval["paid_body_hash"])

    def test_published_at_stored(self):
        self.assertEqual(self.state["published_at"], self.published_at)

    def test_publication_version(self):
        self.assertEqual(self.state["publication_version"], PUBLICATION_VERSION)

    def test_prepared_at_present(self):
        self.assertIn("prepared_at", self.state)

    def test_prepared_at_no_microseconds(self):
        prepared_at = self.state["prepared_at"]
        self.assertNotIn(".", prepared_at)


class TestAdvanceStage(unittest.TestCase):

    def test_prepared_to_pages_verified(self):
        state = _make_pub_state(stage="prepared")
        new_state = advance_stage(state, "pages_verified")
        self.assertEqual(new_state["stage"], "pages_verified")

    def test_pages_verified_to_db_published(self):
        state = _make_pub_state(stage="pages_verified")
        new_state = advance_stage(state, "db_published")
        self.assertEqual(new_state["stage"], "db_published")

    def test_db_published_to_completed(self):
        state = _make_pub_state(stage="db_published")
        new_state = advance_stage(state, "completed")
        self.assertEqual(new_state["stage"], "completed")

    def test_invalid_transition_raises(self):
        state = _make_pub_state(stage="prepared")
        with self.assertRaises(PubStateError):
            advance_stage(state, "db_published")

    def test_skip_stage_raises(self):
        state = _make_pub_state(stage="prepared")
        with self.assertRaises(PubStateError):
            advance_stage(state, "completed")

    def test_stage_at_timestamp_added(self):
        state = _make_pub_state(stage="prepared")
        new_state = advance_stage(state, "pages_verified")
        self.assertIn("pages_verified_at", new_state)

    def test_original_state_not_mutated(self):
        state = _make_pub_state(stage="prepared")
        original_stage = state["stage"]
        advance_stage(state, "pages_verified")
        self.assertEqual(state["stage"], original_stage)

    def test_timestamp_no_microseconds(self):
        state = _make_pub_state(stage="prepared")
        new_state = advance_stage(state, "pages_verified")
        ts = new_state["pages_verified_at"]
        self.assertNotIn(".", ts)


class TestSaveLoadPubState(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_dir = Path(self.tmpdir) / "publications"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _path(self, week_id: str) -> Path:
        return self.state_dir / f"{week_id}_publication.json"

    def test_load_nonexistent_returns_none(self):
        result = load_pub_state("2022-W01", path=self._path("2022-W01"))
        self.assertIsNone(result)

    def test_save_and_load_roundtrip(self):
        state = _make_pub_state()
        path = self._path("2022-W01")
        save_pub_state(state, path=path)
        loaded = load_pub_state("2022-W01", path=path)
        self.assertEqual(loaded, state)

    def test_save_creates_dir(self):
        state = _make_pub_state()
        save_pub_state(state, path=self._path("2022-W01"))
        self.assertTrue(self.state_dir.exists())

    def test_save_dir_permissions(self):
        state = _make_pub_state()
        save_pub_state(state, path=self._path("2022-W01"))
        mode = stat.S_IMODE(self.state_dir.stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_save_file_permissions(self):
        state = _make_pub_state()
        path = self._path("2022-W01")
        save_pub_state(state, path=path)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_save_is_valid_json(self):
        state = _make_pub_state()
        path = self._path("2022-W01")
        save_pub_state(state, path=path)
        content = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(content["week_id"], "2022-W01")

    def test_save_returns_path(self):
        state = _make_pub_state()
        path = self._path("2022-W01")
        result = save_pub_state(state, path=path)
        self.assertEqual(result, path)

    def test_no_tmp_files_left(self):
        state = _make_pub_state()
        save_pub_state(state, path=self._path("2022-W01"))
        tmp_files = list(self.state_dir.glob(".tmp_*"))
        self.assertEqual(tmp_files, [])


# ─── 公開 JSON 構築 ────────────────────────────────────────────────────────────

class TestBuildPublicTeaser(unittest.TestCase):

    def setUp(self):
        self.draft = _make_draft()
        self.approval = _make_approval()
        self.pub_state = _make_pub_state()
        self.teaser = build_public_teaser(self.draft, self.approval, self.pub_state)

    def test_schema_version(self):
        self.assertEqual(self.teaser["schema_version"], SCHEMA_VERSION)

    def test_week_id_from_draft(self):
        self.assertEqual(self.teaser["week_id"], self.draft["week_id"])

    def test_revision_from_draft(self):
        self.assertEqual(self.teaser["revision"], self.draft["revision"])

    def test_published_at_from_pub_state(self):
        self.assertEqual(self.teaser["published_at"], self.pub_state["published_at"])

    def test_teaser_hash_from_approval(self):
        self.assertEqual(self.teaser["teaser_hash"], self.approval["teaser_hash"])

    def test_free_teaser_from_draft(self):
        self.assertEqual(self.teaser["free_teaser"], self.draft["free_teaser"])

    def test_no_paid_body(self):
        self.assertNotIn("paid_body", self.teaser)

    def test_no_paid_body_hash(self):
        self.assertNotIn("paid_body_hash", self.teaser)

    def test_exactly_six_keys(self):
        self.assertEqual(len(self.teaser), 6)


class TestValidatePublicTeaser(unittest.TestCase):

    def setUp(self):
        self.teaser = _make_teaser()

    def test_valid_no_errors(self):
        errors = validate_public_teaser(self.teaser)
        self.assertEqual(errors, [], f"エラー: {errors}")

    def test_wrong_schema_version_rejected(self):
        t = copy.deepcopy(self.teaser)
        t["schema_version"] = 2
        errors = validate_public_teaser(t)
        self.assertTrue(len(errors) > 0)

    def test_invalid_week_id_rejected(self):
        t = copy.deepcopy(self.teaser)
        t["week_id"] = "not-a-week"
        errors = validate_public_teaser(t)
        self.assertTrue(len(errors) > 0)

    def test_invalid_teaser_hash_pattern_rejected(self):
        t = copy.deepcopy(self.teaser)
        t["teaser_hash"] = "ZZZZ"  # uppercase / short
        errors = validate_public_teaser(t)
        self.assertTrue(len(errors) > 0)

    def test_revision_zero_rejected(self):
        t = copy.deepcopy(self.teaser)
        t["revision"] = 0
        errors = validate_public_teaser(t)
        self.assertTrue(len(errors) > 0)

    def test_paid_body_in_free_teaser_rejected(self):
        t = copy.deepcopy(self.teaser)
        t["free_teaser"]["paid_body"] = "secret"
        errors = validate_public_teaser(t)
        self.assertTrue(len(errors) > 0)

    def test_with_featured_theme(self):
        t = copy.deepcopy(self.teaser)
        t["free_teaser"]["featured_theme"] = {
            "tag": "test-tag",
            "label": "テストラベル",
            "summary": "テスト説明文。",
        }
        errors = validate_public_teaser(t)
        self.assertEqual(errors, [], f"エラー: {errors}")

    def test_with_top_match_preview(self):
        t = copy.deepcopy(self.teaser)
        t["free_teaser"]["top_match_preview"] = {
            "event_name": "テストイベント",
            "event_date": "2022-01-01",
        }
        errors = validate_public_teaser(t)
        self.assertEqual(errors, [], f"エラー: {errors}")


# ─── 禁止キー検査 ──────────────────────────────────────────────────────────────

class TestCheckForbiddenKeysDeep(unittest.TestCase):

    def test_clean_object_no_errors(self):
        obj = {"week_id": "2022-W01", "title": "テスト"}
        self.assertEqual(check_forbidden_keys_deep(obj), [])

    def test_paid_body_detected(self):
        errors = check_forbidden_keys_deep({"paid_body": "secret"})
        self.assertTrue(any("paid_body" in e for e in errors))

    def test_paid_body_hash_detected(self):
        errors = check_forbidden_keys_deep({"paid_body_hash": "a" * 64})
        self.assertTrue(any("paid_body_hash" in e for e in errors))

    def test_teaser_hash_allowed(self):
        errors = check_forbidden_keys_deep({"teaser_hash": "a" * 64})
        self.assertEqual(errors, [])

    def test_value_detected(self):
        errors = check_forbidden_keys_deep({"value": 100})
        self.assertTrue(any("value" in e for e in errors))

    def test_nested_forbidden_key_detected(self):
        errors = check_forbidden_keys_deep({"data": {"price": 100}})
        self.assertTrue(any("price" in e for e in errors))

    def test_in_list_detected(self):
        errors = check_forbidden_keys_deep({"items": [{"api_key": "secret"}]})
        self.assertTrue(any("api_key" in e for e in errors))

    def test_jwt_detected(self):
        errors = check_forbidden_keys_deep({"jwt": "token"})
        self.assertTrue(any("jwt" in e for e in errors))

    def test_path_reported_in_error(self):
        errors = check_forbidden_keys_deep({"nested": {"authorization": "Bearer x"}})
        self.assertTrue(any("nested.authorization" in e for e in errors))

    def test_score_detected(self):
        errors = check_forbidden_keys_deep({"score": 0.95})
        self.assertTrue(any("score" in e for e in errors))

    def test_timelines_detected(self):
        errors = check_forbidden_keys_deep({"timelines": []})
        self.assertTrue(any("timelines" in e for e in errors))

    def test_approved_by_detected(self):
        errors = check_forbidden_keys_deep({"approved_by": "someone"})
        self.assertTrue(any("approved_by" in e for e in errors))

    def test_warnings_detected(self):
        errors = check_forbidden_keys_deep({"warnings": []})
        self.assertTrue(any("warnings" in e for e in errors))

    def test_empty_object_no_errors(self):
        self.assertEqual(check_forbidden_keys_deep({}), [])

    def test_empty_list_no_errors(self):
        self.assertEqual(check_forbidden_keys_deep([]), [])


# ─── インデックス管理 ──────────────────────────────────────────────────────────

class TestBuildIndexEntry(unittest.TestCase):

    def setUp(self):
        self.teaser = _make_teaser()
        self.entry = build_index_entry(self.teaser)

    def test_week_id(self):
        self.assertEqual(self.entry["week_id"], self.teaser["week_id"])

    def test_revision(self):
        self.assertEqual(self.entry["revision"], self.teaser["revision"])

    def test_published_at(self):
        self.assertEqual(self.entry["published_at"], self.teaser["published_at"])

    def test_teaser_hash(self):
        self.assertEqual(self.entry["teaser_hash"], self.teaser["teaser_hash"])

    def test_title_from_free_teaser(self):
        self.assertEqual(self.entry["title"], self.teaser["free_teaser"]["title"])

    def test_period_start_from_free_teaser(self):
        self.assertEqual(self.entry["period_start"], self.teaser["free_teaser"]["period_start"])

    def test_env_label_from_free_teaser(self):
        self.assertEqual(self.entry["env_label"], self.teaser["free_teaser"]["env_label"])

    def test_no_paid_body(self):
        self.assertNotIn("paid_body", self.entry)

    def test_exactly_eight_keys(self):
        self.assertEqual(len(self.entry), 8)


class TestUpdateIndex(unittest.TestCase):

    def setUp(self):
        self.teaser = _make_teaser()
        self.entry = build_index_entry(self.teaser)

    def test_add_to_none_creates_index(self):
        new_index, action = update_index(None, self.entry)
        self.assertEqual(action, "added")
        self.assertEqual(len(new_index["reports"]), 1)

    def test_add_to_empty_index(self):
        empty = {"schema_version": 1, "updated_at": "", "latest_week_id": None, "reports": []}
        new_index, action = update_index(empty, self.entry)
        self.assertEqual(action, "added")

    def test_same_content_idempotent(self):
        new_index, _ = update_index(None, self.entry)
        new_index2, action = update_index(new_index, self.entry)
        self.assertEqual(action, "unchanged")
        self.assertEqual(len(new_index2["reports"]), 1)

    def test_different_teaser_hash_raises(self):
        new_index, _ = update_index(None, self.entry)
        conflict = copy.deepcopy(self.entry)
        conflict["teaser_hash"] = "c" * 64
        with self.assertRaises(IndexUpdateError):
            update_index(new_index, conflict)

    def test_latest_week_id_set(self):
        new_index, _ = update_index(None, self.entry)
        self.assertEqual(new_index["latest_week_id"], "2022-W01")

    def test_multiple_weeks_latest_is_newest(self):
        e1 = build_index_entry(_make_teaser("2022-W01"))
        e2 = build_index_entry(_make_teaser("2023-W01"))
        # override teaser_hash to avoid compute_hash collision
        e2["teaser_hash"] = "c" * 64
        idx, _ = update_index(None, e1)
        idx, _ = update_index(idx, e2)
        self.assertEqual(idx["latest_week_id"], "2023-W01")

    def test_reports_sorted_newest_first(self):
        e1 = build_index_entry(_make_teaser("2022-W01"))
        e2 = copy.deepcopy(build_index_entry(_make_teaser("2023-W01")))
        e2["teaser_hash"] = "c" * 64
        idx, _ = update_index(None, e1)
        idx, _ = update_index(idx, e2)
        self.assertEqual(idx["reports"][0]["week_id"], "2023-W01")
        self.assertEqual(idx["reports"][1]["week_id"], "2022-W01")

    def test_updated_at_changes_on_add(self):
        old_idx = {"schema_version": 1, "updated_at": "2020-01-01T00:00:00+00:00",
                   "latest_week_id": None, "reports": []}
        new_idx, _ = update_index(old_idx, self.entry)
        self.assertNotEqual(new_idx["updated_at"], old_idx["updated_at"])

    def test_updated_at_unchanged_when_idempotent(self):
        new_idx, _ = update_index(None, self.entry)
        old_updated = new_idx["updated_at"]
        new_idx2, _ = update_index(new_idx, self.entry)
        self.assertEqual(new_idx2["updated_at"], old_updated)


class TestValidatePublicIndex(unittest.TestCase):

    def _make_index(self, reports=None):
        e = build_index_entry(_make_teaser())
        reports = reports if reports is not None else [e]
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": "2022-01-10T01:00:00+00:00",
            "latest_week_id": "2022-W01",
            "reports": reports,
        }

    def test_valid_index_no_errors(self):
        errors = validate_public_index(self._make_index())
        self.assertEqual(errors, [], f"エラー: {errors}")

    def test_empty_reports_allowed(self):
        idx = self._make_index(reports=[])
        idx["latest_week_id"] = None
        errors = validate_public_index(idx)
        self.assertEqual(errors, [], f"エラー: {errors}")

    def test_wrong_schema_version_rejected(self):
        idx = self._make_index()
        idx["schema_version"] = 99
        errors = validate_public_index(idx)
        self.assertTrue(len(errors) > 0)

    def test_forbidden_key_in_report_detected(self):
        e = build_index_entry(_make_teaser())
        e["paid_body"] = "secret"
        idx = self._make_index(reports=[e])
        errors = validate_public_index(idx)
        self.assertTrue(any("paid_body" in e for e in errors))


# ─── ファイル書き込み ──────────────────────────────────────────────────────────

class TestWritePublicTeaser(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.teaser = _make_teaser()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_returns_written(self):
        path = self.tmpdir / "2022-W01.json"
        result = write_public_teaser(self.teaser, path)
        self.assertEqual(result, "written")

    def test_file_created(self):
        path = self.tmpdir / "2022-W01.json"
        write_public_teaser(self.teaser, path)
        self.assertTrue(path.exists())

    def test_content_roundtrip(self):
        path = self.tmpdir / "2022-W01.json"
        write_public_teaser(self.teaser, path)
        content = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(content, self.teaser)

    def test_idempotent_same_content(self):
        path = self.tmpdir / "2022-W01.json"
        write_public_teaser(self.teaser, path)
        result = write_public_teaser(self.teaser, path)
        self.assertEqual(result, "unchanged")

    def test_different_content_raises(self):
        path = self.tmpdir / "2022-W01.json"
        write_public_teaser(self.teaser, path)
        different = copy.deepcopy(self.teaser)
        different["revision"] = 2
        with self.assertRaises(PublicFileError):
            write_public_teaser(different, path)

    def test_creates_parent_dir(self):
        path = self.tmpdir / "sub" / "2022-W01.json"
        write_public_teaser(self.teaser, path)
        self.assertTrue(path.exists())

    def test_no_tmp_files_left(self):
        path = self.tmpdir / "2022-W01.json"
        write_public_teaser(self.teaser, path)
        tmp_files = list(self.tmpdir.glob(".tmp_*"))
        self.assertEqual(tmp_files, [])


class TestWritePublicIndex(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_and_read(self):
        entry = build_index_entry(_make_teaser())
        idx, _ = update_index(None, entry)
        path = self.tmpdir / "index.json"
        write_public_index(idx, path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, idx)

    def test_overwrite_is_allowed(self):
        e1 = build_index_entry(_make_teaser("2022-W01"))
        idx1, _ = update_index(None, e1)
        path = self.tmpdir / "index.json"
        write_public_index(idx1, path)
        e2 = copy.deepcopy(build_index_entry(_make_teaser("2023-W01")))
        e2["teaser_hash"] = "c" * 64
        idx2, _ = update_index(idx1, e2)
        write_public_index(idx2, path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(loaded["reports"]), 2)


# ─── アーカイブ ────────────────────────────────────────────────────────────────

class TestArchiveDraft(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.draft_dir = self.tmpdir / "drafts"
        self.draft_dir.mkdir()
        self.archive_dir = self.tmpdir / "archives"
        self.draft = _make_draft()
        self.draft_path = self.draft_dir / "2022-W01_draft.json"
        self.draft_path.write_text(
            json.dumps(self.draft, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _archive(self):
        import weekly_pages
        orig = weekly_pages.ARCHIVES_DIR
        weekly_pages.ARCHIVES_DIR = self.archive_dir
        try:
            return archive_draft(self.draft_path, "2022-W01")
        finally:
            weekly_pages.ARCHIVES_DIR = orig

    def test_archive_creates_file(self):
        path = self._archive()
        self.assertTrue(path.exists())

    def test_archive_content_matches(self):
        path = self._archive()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, self.draft)

    def test_archive_dir_permissions(self):
        self._archive()
        mode = stat.S_IMODE(self.archive_dir.stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_archive_file_permissions(self):
        path = self._archive()
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_archive_filename(self):
        path = self._archive()
        self.assertEqual(path.name, "2022-W01_draft.json")

    def test_archive_idempotent(self):
        self._archive()
        path = self._archive()  # 2nd call should not raise
        self.assertTrue(path.exists())


# ─── HTTP 検証 ────────────────────────────────────────────────────────────────

class TestVerifyDeployedTeaser(unittest.TestCase):

    def setUp(self):
        self.teaser = _make_teaser()

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_success_first_try(self, mock_fetch, mock_sleep):
        mock_fetch.return_value = self.teaser
        errors = verify_deployed_teaser("2022-W01", self.teaser, retries=3, interval=1)
        self.assertEqual(errors, [])
        mock_sleep.assert_not_called()

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_success_after_retry(self, mock_fetch, mock_sleep):
        mock_fetch.side_effect = [None, self.teaser]
        errors = verify_deployed_teaser("2022-W01", self.teaser, retries=3, interval=1)
        self.assertEqual(errors, [])
        mock_sleep.assert_called_once_with(1)

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_all_retries_exhausted(self, mock_fetch, mock_sleep):
        mock_fetch.return_value = None
        errors = verify_deployed_teaser("2022-W01", self.teaser, retries=3, interval=1)
        self.assertTrue(len(errors) > 0)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_teaser_hash_mismatch(self, mock_fetch, mock_sleep):
        wrong = copy.deepcopy(self.teaser)
        wrong["teaser_hash"] = "c" * 64
        mock_fetch.return_value = wrong
        errors = verify_deployed_teaser("2022-W01", self.teaser, retries=1, interval=0)
        self.assertTrue(any("teaser_hash" in e for e in errors))

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_week_id_mismatch(self, mock_fetch, mock_sleep):
        wrong = copy.deepcopy(self.teaser)
        wrong["week_id"] = "2023-W01"
        mock_fetch.return_value = wrong
        errors = verify_deployed_teaser("2022-W01", self.teaser, retries=1, interval=0)
        self.assertTrue(any("week_id" in e for e in errors))

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_cache_bust_in_url(self, mock_fetch, mock_sleep):
        mock_fetch.return_value = self.teaser
        verify_deployed_teaser("2022-W01", self.teaser, retries=1, interval=0)
        call_url = mock_fetch.call_args[0][0]
        expected_bust = self.teaser["teaser_hash"][:12]
        self.assertIn(f"?v={expected_bust}", call_url)

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_retry_waits_correctly(self, mock_fetch, mock_sleep):
        mock_fetch.side_effect = [None, None, self.teaser]
        verify_deployed_teaser("2022-W01", self.teaser, retries=3, interval=5)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(5)


class TestVerifyDeployedIndex(unittest.TestCase):

    def setUp(self):
        self.teaser = _make_teaser()
        self.entry = build_index_entry(self.teaser)
        self.index, _ = update_index(None, self.entry)

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_success(self, mock_fetch, mock_sleep):
        mock_fetch.return_value = self.index
        errors = verify_deployed_index("2022-W01", self.entry, retries=1, interval=0)
        self.assertEqual(errors, [])

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_missing_entry(self, mock_fetch, mock_sleep):
        empty_index = {"schema_version": 1, "updated_at": "", "latest_week_id": None, "reports": []}
        mock_fetch.return_value = empty_index
        errors = verify_deployed_index("2022-W01", self.entry, retries=1, interval=0)
        self.assertTrue(any("MISSING" in e for e in errors))

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_teaser_hash_mismatch_in_entry(self, mock_fetch, mock_sleep):
        wrong_index = copy.deepcopy(self.index)
        wrong_index["reports"][0]["teaser_hash"] = "c" * 64
        mock_fetch.return_value = wrong_index
        errors = verify_deployed_index("2022-W01", self.entry, retries=1, interval=0)
        self.assertTrue(any("teaser_hash" in e for e in errors))

    @patch("weekly_pages._sleep")
    @patch("weekly_pages._fetch_json")
    def test_fetch_failure(self, mock_fetch, mock_sleep):
        mock_fetch.return_value = None
        errors = verify_deployed_index("2022-W01", self.entry, retries=1, interval=0)
        self.assertTrue(len(errors) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
