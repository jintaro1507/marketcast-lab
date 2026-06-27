"""
test_weekly_templates.py — weekly_templates.py のユニットテスト

テスト対象:
  - generate_env_label(): 市場環境ラベル生成
  - generate_summary(): 週次要約文生成
  - generate_observation_points(): 観測ポイント生成
  - DISCLAIMER: 免責文言定数
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_templates import (
    DISCLAIMER,
    generate_env_label,
    generate_observation_points,
    generate_summary,
)


# ─── テスト用資産データ helper ────────────────────────────────────────────────

def _make_assets(
    wti_dir="flat",   wti_pct=0.0,
    gold_dir="flat",  gold_pct=0.0,
    sp500_dir="flat", sp500_pct=0.0,
    ust10y_dir="flat", ust10y_pt=0.0,
    usdjpy_dir="flat", usdjpy_pct=0.0,
    vix_dir="flat",   vix_pct=0.0,  vix_lc=None,
    wti_lc=None,
) -> list[dict]:
    return [
        {"asset_key": "wti",    "direction": wti_dir,    "pct_change": wti_pct,    "pt_change": None,      "level_class": wti_lc},
        {"asset_key": "gold",   "direction": gold_dir,   "pct_change": gold_pct,   "pt_change": None,      "level_class": None},
        {"asset_key": "sp500",  "direction": sp500_dir,  "pct_change": sp500_pct,  "pt_change": None,      "level_class": None},
        {"asset_key": "ust10y", "direction": ust10y_dir, "pct_change": None,       "pt_change": ust10y_pt, "level_class": None},
        {"asset_key": "usdjpy", "direction": usdjpy_dir, "pct_change": usdjpy_pct, "pt_change": None,      "level_class": None},
        {"asset_key": "vix",    "direction": vix_dir,    "pct_change": vix_pct,    "pt_change": None,      "level_class": vix_lc},
    ]


# ─── generate_env_label テスト ────────────────────────────────────────────────

class TestGenerateEnvLabel(unittest.TestCase):

    def test_vix_panic(self):
        assets = _make_assets(vix_dir="up", vix_lc="panic")
        self.assertEqual(generate_env_label(assets), "VIX急騰")

    def test_vix_stress_sp500_down(self):
        assets = _make_assets(vix_lc="stress", sp500_dir="down")
        self.assertEqual(generate_env_label(assets), "VIX警戒・株式下落")

    def test_vix_stress_sp500_up(self):
        assets = _make_assets(vix_lc="stress", sp500_dir="up")
        self.assertEqual(generate_env_label(assets), "VIX警戒・株式上昇")

    def test_vix_stress_only(self):
        assets = _make_assets(vix_lc="stress", sp500_dir="flat")
        self.assertEqual(generate_env_label(assets), "VIX警戒")

    def test_gold_up_sp500_down(self):
        assets = _make_assets(gold_dir="up", gold_pct=1.0, sp500_dir="down", sp500_pct=-1.5)
        self.assertEqual(generate_env_label(assets), "安全資産優位")

    def test_sp500_large_up(self):
        assets = _make_assets(sp500_dir="up", sp500_pct=2.5)
        self.assertEqual(generate_env_label(assets), "株式優位")

    def test_wti_large_move(self):
        assets = _make_assets(wti_dir="up", wti_pct=12.0)
        self.assertEqual(generate_env_label(assets), "原油変動拡大")

    def test_rate_up_dollar_up(self):
        assets = _make_assets(ust10y_dir="up", ust10y_pt=0.1, usdjpy_dir="up", usdjpy_pct=0.6)
        self.assertEqual(generate_env_label(assets), "金利上昇・ドル高")

    def test_rate_down(self):
        assets = _make_assets(ust10y_dir="down", ust10y_pt=-0.1)
        self.assertEqual(generate_env_label(assets), "金利低下")

    def test_all_flat(self):
        assets = _make_assets()  # すべて flat/0
        self.assertEqual(generate_env_label(assets), "小動き")

    def test_diverse(self):
        # VIX elev だが stress/panic でない、特定パターンなし
        assets = _make_assets(
            wti_dir="up", wti_pct=1.5,
            gold_dir="up", gold_pct=0.3,
            sp500_dir="up", sp500_pct=0.8,  # sp500<2%なので"株式優位"にならない
            ust10y_dir="up", ust10y_pt=0.03,
        )
        self.assertEqual(generate_env_label(assets), "方向感分散")

    def test_50_chars_or_less(self):
        """env_label が 50 字以内であること。"""
        test_cases = [
            _make_assets(vix_lc="panic"),
            _make_assets(vix_lc="stress", sp500_dir="down"),
            _make_assets(gold_dir="up", sp500_dir="down"),
            _make_assets(sp500_dir="up", sp500_pct=2.5),
            _make_assets(ust10y_dir="up", usdjpy_dir="up"),
        ]
        for assets in test_cases:
            label = generate_env_label(assets)
            self.assertLessEqual(len(label), 50, f"50字超: {label!r}")

    def test_panic_takes_priority_over_gold_sp500(self):
        """VIX panic は gold up / sp500 down より優先される。"""
        assets = _make_assets(
            vix_lc="panic",
            gold_dir="up", gold_pct=1.0,
            sp500_dir="down", sp500_pct=-2.0,
        )
        self.assertEqual(generate_env_label(assets), "VIX急騰")

    def test_stress_priority_over_safe_haven(self):
        """VIX stress は安全資産優位より優先される。"""
        assets = _make_assets(
            vix_lc="stress",
            gold_dir="up", sp500_dir="down",
        )
        self.assertEqual(generate_env_label(assets), "VIX警戒・株式下落")


# ─── generate_summary テスト ─────────────────────────────────────────────────

class TestGenerateSummary(unittest.TestCase):

    def test_vix_panic_pattern(self):
        assets = _make_assets(vix_dir="up", vix_pct=25.0, vix_lc="panic")
        s = generate_summary(assets)
        self.assertIn("VIX", s)
        self.assertIn("急騰", s)

    def test_vix_stress_pattern(self):
        assets = _make_assets(vix_lc="stress")
        s = generate_summary(assets)
        self.assertIn("VIX", s)

    def test_gold_up_sp500_down(self):
        assets = _make_assets(
            gold_dir="up", gold_pct=0.8,
            sp500_dir="down", sp500_pct=-1.1,
        )
        s = generate_summary(assets)
        self.assertIn("金", s)
        self.assertIn("S&P500", s)
        self.assertIn("下落", s)
        self.assertIn("上昇", s)

    def test_ust10y_up(self):
        assets = _make_assets(ust10y_dir="up", ust10y_pt=0.09)
        s = generate_summary(assets)
        self.assertIn("10年債", s)
        self.assertIn("上昇", s)

    def test_ust10y_down(self):
        assets = _make_assets(ust10y_dir="down", ust10y_pt=-0.12)
        s = generate_summary(assets)
        self.assertIn("10年債", s)
        self.assertIn("低下", s)

    def test_wti_large_move(self):
        assets = _make_assets(wti_dir="up", wti_pct=12.5)
        s = generate_summary(assets)
        self.assertIn("WTI", s)

    def test_max_200_chars(self):
        """要約が 200 字以内であること。"""
        assets = _make_assets(
            vix_lc="stress", vix_pct=18.0,
            gold_dir="up", gold_pct=0.8,
            sp500_dir="down", sp500_pct=-1.1,
            ust10y_dir="up", ust10y_pt=0.09,
            wti_dir="up", wti_pct=3.2,
            usdjpy_dir="up", usdjpy_pct=0.5,
        )
        s = generate_summary(assets)
        self.assertLessEqual(len(s), 200, f"200字超: {len(s)}字: {s!r}")

    def test_deterministic(self):
        """同じ入力→同じ出力。"""
        assets = _make_assets(gold_dir="up", gold_pct=1.2, sp500_dir="down", sp500_pct=-0.8)
        s1 = generate_summary(assets)
        s2 = generate_summary(assets)
        self.assertEqual(s1, s2)

    def test_no_duplicate_sentences(self):
        """sp500 と ust10y の文が重複しないこと。"""
        assets = _make_assets(
            gold_dir="up", gold_pct=0.8,
            sp500_dir="down", sp500_pct=-1.1,
            ust10y_dir="up", ust10y_pt=0.09,
        )
        s = generate_summary(assets)
        # sp500 の情報が二度出てこないことを簡易確認
        count = s.count("S&P500")
        self.assertLessEqual(count, 1, f"S&P500が{count}回出現: {s!r}")

    def test_forbidden_expressions_absent(self):
        """禁止表現が含まれないこと。"""
        from weekly_report_builder import FORBIDDEN_EXPRESSIONS
        all_assets_patterns = [
            _make_assets(vix_lc="panic"),
            _make_assets(vix_lc="stress", sp500_dir="down"),
            _make_assets(gold_dir="up", sp500_dir="down"),
            _make_assets(sp500_dir="up", sp500_pct=2.5),
            _make_assets(wti_dir="up", wti_pct=12.0),
            _make_assets(ust10y_dir="up", ust10y_pt=0.1),
            _make_assets(ust10y_dir="down", ust10y_pt=-0.1),
            _make_assets(),  # all flat
        ]
        for assets in all_assets_patterns:
            s = generate_summary(assets)
            for expr in FORBIDDEN_EXPRESSIONS:
                self.assertNotIn(expr, s, f"禁止表現 {expr!r} を検出: {s!r}")

    def test_no_raw_values_in_restricted(self):
        """restricted 資産の生値（end_value等）が含まれないこと。"""
        assets = _make_assets(gold_dir="up", gold_pct=0.8)
        s = generate_summary(assets)
        # end_value は存在しないキー（assets の dir/pct のみ使用）
        self.assertNotIn("end_value", s)

    def test_all_na(self):
        """全資産 na のとき要約が空でないこと。"""
        assets = _make_assets(
            wti_dir="na", wti_pct=None,
            gold_dir="na", gold_pct=None,
            sp500_dir="na", sp500_pct=None,
            ust10y_dir="na", ust10y_pt=None,
            usdjpy_dir="na", usdjpy_pct=None,
            vix_dir="na", vix_pct=None,
        )
        s = generate_summary(assets)
        self.assertTrue(len(s) >= 1)


# ─── generate_observation_points テスト ──────────────────────────────────────

class TestGenerateObservationPoints(unittest.TestCase):

    def _base_assets(self, **kwargs):
        defaults = dict(
            wti_dir="up", wti_pct=3.2,
            ust10y_dir="up", ust10y_pt=0.09,
        )
        defaults.update(kwargs)
        return _make_assets(**defaults)

    def test_returns_3_to_5(self):
        """観測ポイントが 3〜5 件であること。"""
        assets = self._base_assets()
        points = generate_observation_points(assets, [])
        self.assertGreaterEqual(len(points), 3)
        self.assertLessEqual(len(points), 5)

    def test_always_includes_vix_wti_ust10y(self):
        """VIX・WTI・UST10Y の観測が常に含まれること。"""
        assets = self._base_assets()
        points = generate_observation_points(assets, [])
        text = " ".join(points)
        self.assertIn("VIX", text)
        self.assertIn("WTI", text, "WTI observation missing")
        self.assertIn("10年債", text)

    def test_gold_sp500_divergence(self):
        """金と S&P500 が逆方向のとき追加観測ポイントが含まれること。"""
        assets = _make_assets(
            gold_dir="up", gold_pct=0.8,
            sp500_dir="down", sp500_pct=-1.1,
            wti_dir="up", wti_pct=3.2,
            ust10y_dir="up", ust10y_pt=0.09,
        )
        points = generate_observation_points(assets, [])
        text = " ".join(points)
        self.assertIn("金", text)
        self.assertIn("S&P500", text)

    def test_mid_term_reversal_observation(self):
        """mid_term_reversal が見られた類似局面があるとき、その観測が追加されること。"""
        assets = self._base_assets()
        similar_events = [
            {
                "timelines": {
                    "wti": {"d1": "up", "d7": "up", "d30": "down", "d90": "down", "mid_term_reversal": True}
                }
            }
        ]
        points = generate_observation_points(assets, similar_events)
        text = " ".join(points)
        self.assertIn("中期反転", text)

    def test_no_duplicate_points(self):
        """観測ポイントに重複がないこと。"""
        assets = self._base_assets()
        points = generate_observation_points(assets, [])
        self.assertEqual(len(points), len(set(points)))

    def test_neutral_expressions(self):
        """禁止表現が含まれないこと。"""
        from weekly_report_builder import FORBIDDEN_EXPRESSIONS
        assets = _make_assets(
            vix_lc="stress",
            gold_dir="up", sp500_dir="down",
            ust10y_dir="up", ust10y_pt=0.1,
            wti_dir="up", wti_pct=3.2,
        )
        similar_events = [
            {
                "timelines": {
                    "gold": {"d1": "up", "d7": "up", "d30": "down", "d90": "down", "mid_term_reversal": True}
                }
            }
        ]
        points = generate_observation_points(assets, similar_events)
        for p in points:
            for expr in FORBIDDEN_EXPRESSIONS:
                self.assertNotIn(expr, p, f"禁止表現 {expr!r}: {p!r}")


# ─── DISCLAIMER テスト ───────────────────────────────────────────────────────

class TestDisclaimer(unittest.TestCase):

    def test_not_empty(self):
        self.assertTrue(len(DISCLAIMER) >= 1)

    def test_within_500_chars(self):
        self.assertLessEqual(len(DISCLAIMER), 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
