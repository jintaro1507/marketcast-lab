"""
tests/test_weekly_changes.py — 週次差分計算の単体テスト

ネットワーク・DB 不要。stdlib unittest のみ使用。
compute_change() と JSON Schema 検証を中心にテストする。
"""
import datetime
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_config import ASSET_CONFIG_MAP, ASSET_CONFIGS
from calculate_weekly_changes import compute_change

# fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "weekly"
SCHEMA_PATH  = Path(__file__).parent.parent / "schemas" / "weekly_changes.schema.json"


# ── compute_change テスト ────────────────────────────────────────────

class TestComputeChange(unittest.TestCase):
    # WTI: pct_change, flat -1.0 〜 +1.0
    def _wti(self): return ASSET_CONFIG_MAP["wti"]
    def _gold(self): return ASSET_CONFIG_MAP["gold"]
    def _ust10y(self): return ASSET_CONFIG_MAP["ust10y"]
    def _vix(self): return ASSET_CONFIG_MAP["vix"]
    def _usdjpy(self): return ASSET_CONFIG_MAP["usdjpy"]
    def _sp500(self): return ASSET_CONFIG_MAP["sp500"]

    # ── pct 系 ──────────────────────────────────────────────────────

    def test_wti_positive_up(self):
        pct, pt, d = compute_change(self._wti(), 75.0, 70.0)
        self.assertAlmostEqual(pct, 7.14, places=1)
        self.assertIsNone(pt)
        self.assertEqual(d, "up")

    def test_wti_negative_down(self):
        pct, pt, d = compute_change(self._wti(), 70.0, 75.0)
        self.assertAlmostEqual(pct, -6.67, places=1)
        self.assertEqual(d, "down")

    def test_wti_flat_small_positive(self):
        # 0.5% → flat (< 1.0%)
        pct, pt, d = compute_change(self._wti(), 70.35, 70.0)
        self.assertEqual(d, "flat")

    def test_wti_flat_small_negative(self):
        # -0.5% → flat (> -1.0%)
        pct, pt, d = compute_change(self._wti(), 69.65, 70.0)
        self.assertEqual(d, "flat")

    def test_wti_boundary_exact_positive(self):
        # ちょうど +1.0% → up
        pct, pt, d = compute_change(self._wti(), 70.7, 70.0)
        self.assertAlmostEqual(pct, 1.0, places=0)
        self.assertEqual(d, "up")

    def test_wti_boundary_exact_negative(self):
        # ちょうど -1.0% → down
        pct, pt, d = compute_change(self._wti(), 69.3, 70.0)
        self.assertAlmostEqual(pct, -1.0, places=0)
        self.assertEqual(d, "down")

    # Gold: flat -0.5 〜 +0.5
    def test_gold_boundary_above_flat(self):
        # +0.5% → up
        pct, pt, d = compute_change(self._gold(), 1910.0, 1900.52631)
        self.assertEqual(d, "up")

    def test_gold_below_flat(self):
        # -0.5% → down
        pct, pt, d = compute_change(self._gold(), 1890.0, 1900.0)
        self.assertAlmostEqual(pct, -0.53, places=1)
        self.assertEqual(d, "down")

    def test_gold_flat(self):
        # 0.3% → flat
        pct, pt, d = compute_change(self._gold(), 1905.7, 1900.0)
        self.assertEqual(d, "flat")

    # VIX: flat -5.0 〜 +5.0
    def test_vix_large_up(self):
        pct, pt, d = compute_change(self._vix(), 21.0, 18.0)
        self.assertAlmostEqual(pct, 16.67, places=1)
        self.assertEqual(d, "up")

    def test_vix_boundary_flat_high(self):
        # ちょうど +5.0% → up
        pct, pt, d = compute_change(self._vix(), 18.9, 18.0)
        self.assertAlmostEqual(pct, 5.0, places=1)
        self.assertEqual(d, "up")

    def test_vix_inside_flat(self):
        pct, pt, d = compute_change(self._vix(), 18.8, 18.0)
        self.assertEqual(d, "flat")

    # ── pt 系（米10年債）───────────────────────────────────────────

    def test_ust10y_pt_positive_up(self):
        pct, pt, d = compute_change(self._ust10y(), 4.30, 4.20)
        self.assertIsNone(pct)
        self.assertAlmostEqual(pt, 0.1, places=3)
        self.assertEqual(d, "up")

    def test_ust10y_pt_negative_down(self):
        pct, pt, d = compute_change(self._ust10y(), 4.10, 4.20)
        self.assertAlmostEqual(pt, -0.1, places=3)
        self.assertEqual(d, "down")

    def test_ust10y_boundary_flat_high(self):
        # ちょうど +0.05pt → up
        pct, pt, d = compute_change(self._ust10y(), 4.25, 4.20)
        self.assertAlmostEqual(pt, 0.05, places=3)
        self.assertEqual(d, "up")

    def test_ust10y_boundary_flat_low(self):
        # ちょうど -0.05pt → down
        pct, pt, d = compute_change(self._ust10y(), 4.15, 4.20)
        self.assertAlmostEqual(pt, -0.05, places=3)
        self.assertEqual(d, "down")

    def test_ust10y_inside_flat(self):
        pct, pt, d = compute_change(self._ust10y(), 4.24, 4.20)
        self.assertAlmostEqual(pt, 0.04, places=3)
        self.assertEqual(d, "flat")

    def test_ust10y_year_cross(self):
        # 年跨ぎでも計算は同じ
        pct, pt, d = compute_change(self._ust10y(), 4.50, 4.40)
        self.assertAlmostEqual(pt, 0.10, places=3)
        self.assertEqual(d, "up")

    # ── null 入力 ────────────────────────────────────────────────────

    def test_none_current_na(self):
        pct, pt, d = compute_change(self._wti(), None, 70.0)
        self.assertIsNone(pct)
        self.assertIsNone(pt)
        self.assertEqual(d, "na")

    def test_none_previous_na(self):
        pct, pt, d = compute_change(self._wti(), 75.0, None)
        self.assertEqual(d, "na")

    def test_both_none_na(self):
        pct, pt, d = compute_change(self._wti(), None, None)
        self.assertEqual(d, "na")

    def test_previous_zero_na(self):
        pct, pt, d = compute_change(self._wti(), 75.0, 0.0)
        self.assertEqual(d, "na")
        self.assertIsNone(pct)


# ── JSON Schema テスト ───────────────────────────────────────────────

class TestChangesSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import jsonschema  # noqa: F401
            from jsonschema.validators import validator_for
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            cls._schema  = schema
            cls._vcls    = validator_for(schema)
            cls._skip    = False
        except ImportError:
            cls._skip = True

    def _validate(self, data):
        if self._skip:
            self.skipTest("jsonschema not available")
        return list(self._vcls(self._schema).iter_errors(data))

    def test_valid_fixture_passes(self):
        data = json.loads((FIXTURES_DIR / "changes_valid.json").read_text())
        errors = self._validate(data)
        self.assertEqual(errors, [], [str(e) for e in errors])

    def test_invalid_with_value_fails(self):
        data = json.loads((FIXTURES_DIR / "changes_invalid_with_value.json").read_text())
        errors = self._validate(data)
        self.assertTrue(len(errors) > 0, "current_value が含まれているのにスキーマが通ってしまった")

    def test_no_current_value_in_schema_properties(self):
        # properties に current_value / previous_value が定義されていないことを確認
        # (description 文中に言及があっても問題なし)
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        asset_props = schema["$defs"]["AssetChange"]["properties"]
        self.assertNotIn("current_value",  asset_props)
        self.assertNotIn("previous_value", asset_props)
        self.assertNotIn("value",          asset_props)

    def test_six_assets_required(self):
        data = json.loads((FIXTURES_DIR / "changes_valid.json").read_text())
        data["assets"] = data["assets"][:5]  # 5件に減らす
        errors = self._validate(data)
        self.assertTrue(len(errors) > 0)

    def test_additional_properties_top_rejected(self):
        data = json.loads((FIXTURES_DIR / "changes_valid.json").read_text())
        data["unexpected_field"] = "should_fail"
        errors = self._validate(data)
        self.assertTrue(len(errors) > 0)


# ── 欠損・HARD/WARN 統合テスト ───────────────────────────────────────

class TestComputeChangeAllAssets(unittest.TestCase):
    """全資産で compute_change が正常に動作することを確認する。"""

    def test_all_assets_flat_zero_change(self):
        for cfg in ASSET_CONFIGS:
            pct, pt, d = compute_change(cfg, 100.0, 100.0)
            self.assertEqual(d, "flat", f"{cfg['asset_key']}: expected flat for 0 change")

    def test_all_assets_large_positive(self):
        for cfg in ASSET_CONFIGS:
            pct, pt, d = compute_change(cfg, 200.0, 100.0)
            self.assertEqual(d, "up", f"{cfg['asset_key']}: expected up for 100% increase")

    def test_all_assets_large_negative(self):
        for cfg in ASSET_CONFIGS:
            pct, pt, d = compute_change(cfg, 50.0, 100.0)
            self.assertEqual(d, "down", f"{cfg['asset_key']}: expected down for -50%")

    def test_pct_vs_pt_type(self):
        wti_pct, wti_pt, _ = compute_change(ASSET_CONFIG_MAP["wti"], 75.0, 70.0)
        self.assertIsNotNone(wti_pct)
        self.assertIsNone(wti_pt)

        ust_pct, ust_pt, _ = compute_change(ASSET_CONFIG_MAP["ust10y"], 4.30, 4.20)
        self.assertIsNone(ust_pct)
        self.assertIsNotNone(ust_pt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
