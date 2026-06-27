"""
tests/test_weekly_db_local.py — ローカル Supabase DB 統合テスト

実行条件:
  - SUPABASE_TEST_URL と SUPABASE_TEST_SERVICE_KEY が環境変数で設定されていること
  - または ローカル Supabase がデフォルトポートで起動していること

本番 Supabase URL が設定されている場合はスキップする（ProductionGuardError で拒否）。

実行方法:
  SUPABASE_TEST_URL=http://127.0.0.1:54321 \\
  SUPABASE_TEST_SERVICE_KEY=eyJ... \\
  python -m unittest tests/test_weekly_db_local.py -v

または デフォルト URL を使用:
  python -m unittest tests/test_weekly_db_local.py -v
"""
import datetime
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_config import ASSET_CONFIGS, PROD_PROJECT_REF, is_restricted
from weekly_db import WeeklyDB, WeeklyDBError, ProductionGuardError
from weekly_snapshot import build_snapshot_record

# ローカル Supabase のデフォルト値（公開されている開発用固定値）
_LOCAL_URL     = "http://127.0.0.1:54321"
_LOCAL_SVC_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0"
    ".EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)

TEST_URL = os.environ.get("SUPABASE_TEST_URL", _LOCAL_URL)
TEST_KEY = os.environ.get("SUPABASE_TEST_SERVICE_KEY", _LOCAL_SVC_KEY)

# テスト用の過去 ISO 週（本番データと衝突しない）
TEST_WEEK_ID = "2019-W01"
PERIOD_START = datetime.date(2018, 12, 31)
PERIOD_END   = datetime.date(2019, 1,  4)
TAKEN_AT     = "2019-01-05T00:00:00+00:00"


def _make_records(week_id=TEST_WEEK_ID, seeded=False, seed_source=None):
    records = []
    for cfg in ASSET_CONFIGS:
        obs = ("2019-01-04", 100.0)
        rec = build_snapshot_record(
            cfg, obs, f"test_{cfg['asset_key']}", TAKEN_AT,
            week_id, seeded=seeded, seed_source=seed_source,
        )
        records.append(rec)
    return records


def _skip_if_production(url: str, test_case: unittest.TestCase) -> None:
    if PROD_PROJECT_REF in url:
        test_case.skipTest(
            f"本番 Supabase URL が設定されています。テストをスキップします。"
            f"ローカル Supabase を使用してください。"
        )


class TestLocalDBUpsert(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_if_production(TEST_URL, cls)
        cls.db = WeeklyDB(TEST_URL, TEST_KEY)

    def setUp(self):
        # テスト前にクリーンアップ
        try:
            self.db.delete_snapshots(TEST_WEEK_ID)
        except Exception:
            pass

    def tearDown(self):
        # テスト後にクリーンアップ
        try:
            self.db.delete_snapshots(TEST_WEEK_ID)
        except Exception:
            pass

    def test_upsert_six_records(self):
        records = _make_records()
        saved = self.db.upsert_snapshots(records)
        self.assertEqual(saved, 6)

    def test_upsert_idempotent(self):
        records = _make_records()
        self.db.upsert_snapshots(records)
        # 同じ week_id で再実行 → エラーにならない（upsert）
        saved = self.db.upsert_snapshots(records)
        self.assertEqual(saved, 6)

    def test_get_snapshots_after_upsert(self):
        self.db.upsert_snapshots(_make_records())
        rows = self.db.get_snapshots(TEST_WEEK_ID)
        self.assertEqual(len(rows), 6)

    def test_seed_upsert(self):
        records = _make_records(seeded=True, seed_source="initial_seed_from_source_series")
        saved = self.db.upsert_snapshots(records)
        self.assertEqual(saved, 6)
        rows = self.db.get_snapshots(TEST_WEEK_ID)
        seeded_count = sum(1 for r in rows if r.get("seeded"))
        self.assertEqual(seeded_count, 6)

    def test_restricted_constraint_gold_false_fails(self):
        records = _make_records()
        # gold の restricted を False に改ざん → DB の CHECK 制約で失敗するはず
        for r in records:
            if r["asset_key"] == "gold":
                r["restricted"] = False
        with self.assertRaises(WeeklyDBError):
            self.db.upsert_snapshots(records)

    def test_restricted_constraint_wti_true_fails(self):
        records = _make_records()
        for r in records:
            if r["asset_key"] == "wti":
                r["restricted"] = True
        with self.assertRaises(WeeklyDBError):
            self.db.upsert_snapshots(records)

    def test_partial_failure_rolls_back(self):
        """1件の制約違反が全件ロールバックされることを確認する。"""
        records = _make_records()
        for r in records:
            if r["asset_key"] == "gold":
                r["restricted"] = False  # 意図的に違反
        try:
            self.db.upsert_snapshots(records)
        except WeeklyDBError:
            pass
        # ロールバックにより 0 件であるべき
        rows = self.db.get_snapshots(TEST_WEEK_ID)
        self.assertEqual(len(rows), 0, "部分的な upsert が発生しました（ロールバック失敗）")

    def test_record_count_after_save(self):
        self.db.upsert_snapshots(_make_records())
        rows = self.db.get_snapshots(TEST_WEEK_ID)
        self.assertEqual(len(rows), 6)

    def test_production_guard_blocks_without_flag(self):
        """本番 URL は allow_production=False（デフォルト）で拒否される。"""
        prod_db = WeeklyDB(
            f"https://{PROD_PROJECT_REF}.supabase.co",
            "dummy_key"
        )
        with self.assertRaises(ProductionGuardError):
            prod_db.upsert_snapshots(_make_records())


class TestLocalDBRLS(unittest.TestCase):
    """anon キーでは SELECT/INSERT が拒否されることを確認する。"""

    _LOCAL_ANON_KEY = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9"
        ".CRXP1A7WOeoJeXxjNni43kdQwgnWNReilDMblYTn_I0"
    )

    @classmethod
    def setUpClass(cls):
        _skip_if_production(TEST_URL, cls)
        # anon キーで WeeklyDB を作る（anon は RLS で弾かれるはず）
        cls.anon_db = WeeklyDB(TEST_URL, cls._LOCAL_ANON_KEY)

    def test_anon_select_blocked(self):
        with self.assertRaises(WeeklyDBError):
            self.anon_db.get_snapshots(TEST_WEEK_ID)

    def test_anon_insert_blocked(self):
        records = _make_records()
        with self.assertRaises((WeeklyDBError, ProductionGuardError)):
            # anon_db は allow_production=True でも RLS で弾かれるはず
            # ProductionGuardError が出ることはないが念のため両方キャッチ
            self.anon_db.upsert_snapshots(records, allow_production=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
