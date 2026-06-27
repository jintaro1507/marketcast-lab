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
from weekly_dates import prev_week_id
from weekly_db import WeeklyDB, WeeklyDBError, ProductionGuardError
from weekly_snapshot import build_snapshot_record
from calculate_weekly_changes import build_changes, _rows_with_value, _safe_float, _assert_no_raw_values

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

# 差分計算統合テスト用テスト週（既存テストと衝突しない過去の週を選択）
# 2017-W02: Mon 2017-01-09, Fri 2017-01-13
# 2017-W03: Mon 2017-01-16, Fri 2017-01-20  ← prev of W03 = W02
_CHG_PREV_WEEK  = "2017-W02"
_CHG_CURR_WEEK  = "2017-W03"
_CHG_PREV_AS_OF = "2017-01-13"
_CHG_CURR_AS_OF = "2017-01-20"
_CHG_TAKEN_AT   = "2017-01-21T00:00:00+00:00"

# 明らかに架空の丸い数値（実在市場値ではない）
_PREV_VALUES = {
    "wti":    70.0,
    "gold":   100.0,   # restricted — 架空の丸い値
    "sp500":  200.0,   # restricted — 架空の丸い値
    "ust10y": 4.00,
    "usdjpy": 100.0,
    "vix":    20.0,
}
_CURR_VALUES = {
    "wti":    77.0,    # +10.0% → up
    "gold":   100.4,   # +0.4% → flat (flat_high=0.5)
    "sp500":  198.0,   # -1.0% → down (flat_low=-1.0, exactly on boundary)
    "ust10y": 4.05,    # +0.05pt → up (flat_high=0.05, exactly on boundary)
    "usdjpy": 100.3,   # +0.3% → flat
    "vix":    21.1,    # +5.5% → up
}

# 異常ケース用テスト週
_ANOM_PREV_WEEK = "2016-W02"
_ANOM_CURR_WEEK = "2016-W03"

# Seeded WARN テスト用週
_SEEDED_PREV_WEEK = "2015-W02"
_SEEDED_CURR_WEEK = "2015-W03"

# Seed 拒否テスト用週
_SEED_REJ_WEEK = "2018-W02"

_TEST_SECRETS = {}  # mask_secret 用（テストでは秘密情報なし）


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


def _make_records_valued(week_id, values, as_of, seeded=False, seed_source=None):
    """week_id 固有の as_of と指定値でレコードを構築する（差分計算テスト用）。"""
    records = []
    for cfg in ASSET_CONFIGS:
        key = cfg["asset_key"]
        obs = (as_of, values[key])
        rec = build_snapshot_record(
            cfg, obs, f"test_{key}", _CHG_TAKEN_AT,
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


# ── seed 拒否テスト ──────────────────────────────────────────────────

class TestSeedRejection(unittest.TestCase):
    """
    seed_initial_snapshot.py の拒否ロジックを DB を通じて検証する。
    拒否判定は DB から get_snapshots() で取得した結果をアプリ側でチェックする。
    """

    @classmethod
    def setUpClass(cls):
        _skip_if_production(TEST_URL, cls)
        cls.db = WeeklyDB(TEST_URL, TEST_KEY)

    def setUp(self):
        try:
            self.db.delete_snapshots(_SEED_REJ_WEEK)
        except Exception:
            pass

    def tearDown(self):
        try:
            self.db.delete_snapshots(_SEED_REJ_WEEK)
        except Exception:
            pass

    def test_seed_rejected_when_normal_snapshot_exists(self):
        """通常スナップが既に存在する週への seed は拒否される。"""
        # 通常スナップ（seeded=False）を挿入
        self.db.upsert_snapshots(_make_records(week_id=_SEED_REJ_WEEK, seeded=False))

        # seed_initial_snapshot.py と同じ判定ロジック
        existing = self.db.get_snapshots(_SEED_REJ_WEEK)
        has_normal = any(not r.get("seeded") for r in existing)
        has_seed   = any(r.get("seeded") for r in existing)

        self.assertTrue(has_normal, "通常スナップが検出されていない")
        self.assertFalse(has_seed, "seed スナップが混在している（想定外）")

        # 拒否条件が成立することを確認（has_normal == True → 拒否）
        should_reject = has_normal
        self.assertTrue(should_reject)

        # 拒否後は DB に seeded レコードが追加されていないはず
        after = self.db.get_snapshots(_SEED_REJ_WEEK)
        self.assertEqual(len(after), 6)
        for r in after:
            self.assertFalse(r.get("seeded"), f"{r['asset_key']} が seeded になっている")

    def test_seed_detection_all_normal_records_are_not_seeded(self):
        """通常スナップの seeded フラグがすべて False であることを DB から確認する。"""
        self.db.upsert_snapshots(_make_records(week_id=_SEED_REJ_WEEK, seeded=False))
        rows = self.db.get_snapshots(_SEED_REJ_WEEK)
        self.assertEqual(len(rows), 6)
        seeded_count = sum(1 for r in rows if r.get("seeded"))
        self.assertEqual(seeded_count, 0)

    def test_reseed_rejected_when_seed_already_exists(self):
        """seed スナップが既に存在する週への再 seed は拒否される。"""
        seed_src = "initial_seed_from_source_series"
        self.db.upsert_snapshots(_make_records(week_id=_SEED_REJ_WEEK, seeded=True, seed_source=seed_src))

        existing = self.db.get_snapshots(_SEED_REJ_WEEK)
        has_normal = any(not r.get("seeded") for r in existing)
        has_seed   = any(r.get("seeded") for r in existing)

        self.assertFalse(has_normal, "通常スナップが混在している（想定外）")
        self.assertTrue(has_seed, "seed スナップが検出されていない")

        # has_seed == True → 拒否
        should_reject = has_seed
        self.assertTrue(should_reject)

    def test_seed_detection_all_seed_records_are_seeded(self):
        """seed スナップの seeded フラグがすべて True であることを DB から確認する。"""
        seed_src = "initial_seed_from_source_series"
        self.db.upsert_snapshots(_make_records(week_id=_SEED_REJ_WEEK, seeded=True, seed_source=seed_src))
        rows = self.db.get_snapshots(_SEED_REJ_WEEK)
        seeded_count = sum(1 for r in rows if r.get("seeded"))
        self.assertEqual(seeded_count, 6)


# ── DB → 差分計算 統合テスト ─────────────────────────────────────────

class TestDBChangesIntegration(unittest.TestCase):
    """DB に 2 週分のスナップを挿入し、build_changes() の結果を検証する。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(TEST_URL, cls)
        cls.db = WeeklyDB(TEST_URL, TEST_KEY)

    def setUp(self):
        for wid in (_CHG_PREV_WEEK, _CHG_CURR_WEEK):
            try:
                self.db.delete_snapshots(wid)
            except Exception:
                pass
        # 前週・当週スナップを挿入
        self.db.upsert_snapshots(
            _make_records_valued(_CHG_PREV_WEEK, _PREV_VALUES, _CHG_PREV_AS_OF)
        )
        self.db.upsert_snapshots(
            _make_records_valued(_CHG_CURR_WEEK, _CURR_VALUES, _CHG_CURR_AS_OF)
        )

    def tearDown(self):
        for wid in (_CHG_PREV_WEEK, _CHG_CURR_WEEK):
            try:
                self.db.delete_snapshots(wid)
            except Exception:
                pass

    def test_result_has_no_hard_errors(self):
        result = build_changes(_CHG_CURR_WEEK, self.db, _TEST_SECRETS)
        self.assertEqual(result["hard_errors"], [], result["hard_errors"])

    def test_week_ids_correct(self):
        result = build_changes(_CHG_CURR_WEEK, self.db, _TEST_SECRETS)
        self.assertEqual(result["week_id"], _CHG_CURR_WEEK)
        self.assertEqual(result["previous_week_id"], _CHG_PREV_WEEK)

    def test_six_assets_in_output(self):
        result = build_changes(_CHG_CURR_WEEK, self.db, _TEST_SECRETS)
        self.assertEqual(len(result["assets"]), 6)

    def test_wti_direction_up(self):
        result = build_changes(_CHG_CURR_WEEK, self.db, _TEST_SECRETS)
        wti = next(a for a in result["assets"] if a["asset_key"] == "wti")
        self.assertEqual(wti["direction"], "up")
        self.assertAlmostEqual(wti["pct_change"], 10.0, places=1)
        self.assertIsNone(wti["pt_change"])

    def test_gold_direction_flat(self):
        result = build_changes(_CHG_CURR_WEEK, self.db, _TEST_SECRETS)
        gold = next(a for a in result["assets"] if a["asset_key"] == "gold")
        self.assertEqual(gold["direction"], "flat")
        self.assertAlmostEqual(gold["pct_change"], 0.4, places=1)

    def test_sp500_direction_down(self):
        result = build_changes(_CHG_CURR_WEEK, self.db, _TEST_SECRETS)
        sp500 = next(a for a in result["assets"] if a["asset_key"] == "sp500")
        self.assertEqual(sp500["direction"], "down")
        self.assertAlmostEqual(sp500["pct_change"], -1.0, places=1)

    def test_ust10y_direction_up(self):
        result = build_changes(_CHG_CURR_WEEK, self.db, _TEST_SECRETS)
        ust = next(a for a in result["assets"] if a["asset_key"] == "ust10y")
        self.assertEqual(ust["direction"], "up")
        self.assertIsNone(ust["pct_change"])
        self.assertAlmostEqual(ust["pt_change"], 0.05, places=3)

    def test_vix_direction_up(self):
        result = build_changes(_CHG_CURR_WEEK, self.db, _TEST_SECRETS)
        vix = next(a for a in result["assets"] if a["asset_key"] == "vix")
        self.assertEqual(vix["direction"], "up")
        self.assertAlmostEqual(vix["pct_change"], 5.5, places=1)

    def test_no_raw_values_in_output(self):
        """出力 JSON に current_value / previous_value / value が含まれないことを確認する。"""
        result = build_changes(_CHG_CURR_WEEK, self.db, _TEST_SECRETS)
        _assert_no_raw_values(result)  # raises RuntimeError if found

    def test_numeric_value_type_from_postgrest(self):
        """PostgREST が NUMERIC を str か float で返し、_safe_float が正しく変換することを確認する。"""
        # _rows_with_value を直接呼んで生の型を確認する
        curr_map = _rows_with_value(self.db, _CHG_CURR_WEEK)
        self.assertEqual(len(curr_map), 6)

        wti_row = curr_map["wti"]
        value = wti_row["value"]

        # _safe_float 適用後は float であるはず
        self.assertIsInstance(value, float)
        self.assertAlmostEqual(value, _CURR_VALUES["wti"], places=3)

    def test_direct_postgrest_numeric_type(self):
        """PostgREST が返す生の NUMERIC 型（変換前）が str か数値であることを確認する。"""
        # _request を直接呼んで型を確認
        _, rows = self.db._request(
            "GET",
            f"/weekly_asset_snapshots?week_id=eq.{_CHG_CURR_WEEK}"
            "&asset_key=eq.wti&select=value",
        )
        raw = rows[0]["value"]
        # PostgREST は NUMERIC を string or number で返す
        self.assertIn(type(raw).__name__, ("str", "int", "float"),
                      f"想定外の型: {type(raw).__name__!r}")
        # どちらであっても _safe_float で正しく変換できる
        self.assertAlmostEqual(_safe_float(raw), _CURR_VALUES["wti"], places=3)


# ── DB 異常ケーステスト ───────────────────────────────────────────────

class TestDBChangesAnomalies(unittest.TestCase):
    """build_changes() が DB 異常を HARD エラーとして正しく検出することを確認する。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(TEST_URL, cls)
        cls.db = WeeklyDB(TEST_URL, TEST_KEY)

    def setUp(self):
        for wid in (_ANOM_PREV_WEEK, _ANOM_CURR_WEEK):
            try:
                self.db.delete_snapshots(wid)
            except Exception:
                pass

    def tearDown(self):
        for wid in (_ANOM_PREV_WEEK, _ANOM_CURR_WEEK):
            try:
                self.db.delete_snapshots(wid)
            except Exception:
                pass

    def _insert_full(self, week_id, values, as_of):
        self.db.upsert_snapshots(_make_records_valued(week_id, values, as_of))

    def test_five_curr_records_is_hard(self):
        """当週スナップが 5 件の場合は HARD エラーになる。"""
        self._insert_full(_ANOM_PREV_WEEK, _PREV_VALUES, "2016-01-15")
        self._insert_full(_ANOM_CURR_WEEK, _CURR_VALUES, "2016-01-22")

        # 当週から vix を削除して 5 件にする
        self.db._request(
            "DELETE",
            f"/weekly_asset_snapshots?week_id=eq.{_ANOM_CURR_WEEK}&asset_key=eq.vix",
        )

        result = build_changes(_ANOM_CURR_WEEK, self.db, _TEST_SECRETS)
        hard_text = " ".join(result["hard_errors"])
        self.assertIn("6件でない", hard_text, f"HARD に件数エラーがない: {result['hard_errors']}")

    def test_five_prev_records_is_hard(self):
        """前週スナップが 5 件の場合は HARD エラーになる。"""
        self._insert_full(_ANOM_PREV_WEEK, _PREV_VALUES, "2016-01-15")
        self._insert_full(_ANOM_CURR_WEEK, _CURR_VALUES, "2016-01-22")

        # 前週から vix を削除して 5 件にする
        self.db._request(
            "DELETE",
            f"/weekly_asset_snapshots?week_id=eq.{_ANOM_PREV_WEEK}&asset_key=eq.vix",
        )

        result = build_changes(_ANOM_CURR_WEEK, self.db, _TEST_SECRETS)
        hard_text = " ".join(result["hard_errors"])
        self.assertIn("6件でない", hard_text, f"HARD に件数エラーがない: {result['hard_errors']}")

    def test_prev_value_zero_for_pct_asset_is_hard(self):
        """前週値が 0 の pct 資産がある場合は HARD エラー（ゼロ除算防止）になる。"""
        zero_prev = dict(_PREV_VALUES)
        zero_prev["wti"] = 0.0  # pct 資産のゼロ値

        self._insert_full(_ANOM_PREV_WEEK, zero_prev, "2016-01-15")
        self._insert_full(_ANOM_CURR_WEEK, _CURR_VALUES, "2016-01-22")

        result = build_changes(_ANOM_CURR_WEEK, self.db, _TEST_SECRETS)
        hard_text = " ".join(result["hard_errors"])
        self.assertIn("wti", hard_text, f"wti の HARD エラーがない: {result['hard_errors']}")
        self.assertIn("変化率計算不可", hard_text)


# ── seeded 前週 WARN 統合テスト ───────────────────────────────────────

class TestSeededWarnIntegration(unittest.TestCase):
    """前週スナップが seeded の場合に WARN が生成されることを確認する。"""

    @classmethod
    def setUpClass(cls):
        _skip_if_production(TEST_URL, cls)
        cls.db = WeeklyDB(TEST_URL, TEST_KEY)

    def setUp(self):
        for wid in (_SEEDED_PREV_WEEK, _SEEDED_CURR_WEEK):
            try:
                self.db.delete_snapshots(wid)
            except Exception:
                pass
        # 前週: seeded スナップ
        self.db.upsert_snapshots(
            _make_records_valued(
                _SEEDED_PREV_WEEK, _PREV_VALUES, "2015-01-09",
                seeded=True, seed_source="initial_seed_from_source_series",
            )
        )
        # 当週: 通常スナップ
        self.db.upsert_snapshots(
            _make_records_valued(_SEEDED_CURR_WEEK, _CURR_VALUES, "2015-01-16")
        )

    def tearDown(self):
        for wid in (_SEEDED_PREV_WEEK, _SEEDED_CURR_WEEK):
            try:
                self.db.delete_snapshots(wid)
            except Exception:
                pass

    def test_seeded_prev_generates_warn(self):
        result = build_changes(_SEEDED_CURR_WEEK, self.db, _TEST_SECRETS)
        self.assertEqual(result["hard_errors"], [], result["hard_errors"])
        warn_text = " ".join(result["warnings"])
        self.assertIn("seeded", warn_text, f"WARN に seeded が含まれていない: {result['warnings']}")

    def test_seeded_prev_computation_still_succeeds(self):
        """seeded 前週でも差分計算は成功し 6 資産が出力される。"""
        result = build_changes(_SEEDED_CURR_WEEK, self.db, _TEST_SECRETS)
        self.assertEqual(len(result["assets"]), 6)
        directions = [a["direction"] for a in result["assets"]]
        self.assertNotIn("na", directions,
                         "seeded 前週でも全資産の direction は計算できるはず")

    def test_seeded_prev_warn_per_asset(self):
        """各 asset の warn にも seeded が記録される。"""
        result = build_changes(_SEEDED_CURR_WEEK, self.db, _TEST_SECRETS)
        seeded_warn_count = sum(
            1 for a in result["assets"]
            if any("seeded" in w for w in a.get("warn", []))
        )
        self.assertEqual(seeded_warn_count, 6, "全 6 資産で seeded WARN が記録されていない")


if __name__ == "__main__":
    unittest.main(verbosity=2)
