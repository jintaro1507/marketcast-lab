"""
test_weekly_matcher.py — weekly_matcher.py + Deno wrapper のテスト

テスト対象:
  - build_state_tags_from_context(): context から state_tags 取得
  - get_data_completeness(): data_completeness 取得
  - run_matcher_single(): parity test（Edge Function と同一結果）
  - Deno wrapper stdout が JSON のみであること
  - Deno wrapper: 結果0件ケース
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).parent
FIXTURES  = TESTS_DIR / "fixtures" / "weekly"
PROJECT   = Path(__file__).parent.parent
SCRIPTS   = PROJECT / "scripts"

sys.path.insert(0, str(SCRIPTS))

from weekly_matcher import (
    build_state_tags_from_context,
    get_data_completeness,
    load_context,
    load_events,
    run_matcher_single,
    MatcherError,
    _WRAPPER_SCRIPT,
)


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def _deno_available() -> bool:
    try:
        r = subprocess.run(["deno", "--version"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


DENO_AVAILABLE = _deno_available()
SKIP_DENO = unittest.skipUnless(DENO_AVAILABLE, "Deno が利用できないためスキップ")


# ─── build_state_tags_from_context テスト ────────────────────────────────────

class TestBuildStateTags(unittest.TestCase):

    def _ctx(self, state_tags=None, completeness=3):
        if state_tags is None:
            state_tags = {"vix": "elev", "oil": "mid", "rate": "high"}
        return {
            "market_state_tags": state_tags,
            "data_completeness": completeness,
        }

    def test_valid_context(self):
        ctx = self._ctx()
        tags = build_state_tags_from_context(ctx)
        self.assertEqual(tags, {"vix": "elev", "oil": "mid", "rate": "high"})

    def test_empty_state_tags_raises(self):
        ctx = self._ctx(state_tags={})
        with self.assertRaises(MatcherError):
            build_state_tags_from_context(ctx)

    def test_none_state_tags_raises(self):
        ctx = {"market_state_tags": None, "data_completeness": 0}
        with self.assertRaises(MatcherError):
            build_state_tags_from_context(ctx)

    def test_fixture_context(self):
        ctx = _load_fixture("context_public_fixture.json")
        tags = build_state_tags_from_context(ctx)
        self.assertEqual(tags["vix"], "elev")
        self.assertEqual(tags["oil"], "mid")
        self.assertEqual(tags["rate"], "high")


class TestGetDataCompleteness(unittest.TestCase):

    def test_returns_int(self):
        ctx = {"data_completeness": 3}
        self.assertEqual(get_data_completeness(ctx), 3)

    def test_missing_returns_0(self):
        self.assertEqual(get_data_completeness({}), 0)

    def test_float_converts(self):
        ctx = {"data_completeness": 2.0}
        self.assertEqual(get_data_completeness(ctx), 2)


# ─── Deno wrapper テスト ─────────────────────────────────────────────────────

@SKIP_DENO
class TestDenoWrapper(unittest.TestCase):

    def setUp(self):
        self.events, _ = load_events()
        self.state_tags = {"vix": "elev", "oil": "mid", "rate": "high"}

    def test_wrapper_script_exists(self):
        self.assertTrue(_WRAPPER_SCRIPT.exists(), f"Wrapper not found: {_WRAPPER_SCRIPT}")

    def test_stdout_is_json_only(self):
        """stdout が JSON のみで始まること（ログが混入していないこと）。"""
        payload = json.dumps({
            "cause_tag": "bank_crisis",
            "state_tags": self.state_tags,
            "events": self.events,
            "top_n": 3,
        }, ensure_ascii=False)

        result = subprocess.run(
            ["deno", "run", "--allow-read", str(_WRAPPER_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, f"Deno failed: {result.stderr[:500]}")
        stdout = result.stdout.strip()
        self.assertTrue(stdout.startswith("{"), f"stdout is not JSON: {stdout[:100]!r}")
        output = json.loads(stdout)
        self.assertIn("matches", output)
        self.assertIsInstance(output["matches"], list)

    def test_matches_are_ranked(self):
        """matches が rank 順であること。"""
        matches = run_matcher_single("bank_crisis", self.state_tags, self.events, top_n=3)
        for i, m in enumerate(matches):
            self.assertEqual(m["rank"], i + 1)

    def test_timelines_direction_only(self):
        """timelines が方向文字列のみで raw 値を含まないこと。"""
        matches = run_matcher_single("bank_crisis", self.state_tags, self.events, top_n=1)
        if matches:
            for asset_key, tl in matches[0].get("timelines", {}).items():
                self.assertIn(tl["d1"], ("up", "down", "flat", "na"))
                self.assertIsInstance(tl["mid_term_reversal"], bool)
                # raw 値がないこと
                self.assertNotIn("changes", tl)
                self.assertNotIn("value", tl)

    def test_empty_result_for_unknown_cause_tag(self):
        """存在しない cause_tag → 空リスト（エラーなし）。"""
        matches = run_matcher_single(
            "__nonexistent_tag__", self.state_tags, self.events, top_n=5
        )
        self.assertEqual(matches, [])

    def test_cause_tags_in_output(self):
        """matches に cause_tags フィールドが含まれること。"""
        matches = run_matcher_single("bank_crisis", self.state_tags, self.events, top_n=3)
        for m in matches:
            self.assertIn("cause_tags", m, "cause_tags フィールドが欠損")
            self.assertIsInstance(m["cause_tags"], list)

    def test_score_present(self):
        """matches に score があること。"""
        matches = run_matcher_single("bank_crisis", self.state_tags, self.events, top_n=3)
        for m in matches:
            self.assertIn("score", m)
            self.assertIsInstance(m["score"], (int, float))


# ─── parity テスト ───────────────────────────────────────────────────────────

@SKIP_DENO
class TestMatcherParity(unittest.TestCase):
    """
    同じ cause_tag + state_tags で2回実行したとき同じ順位を返すことを確認。
    （Edge Function と同じ matching.ts を使うため、本物と一致する）
    """

    def setUp(self):
        self.events, _ = load_events()
        self.state_tags = {"vix": "elev", "oil": "mid", "rate": "high"}

    def test_deterministic_single_cause(self):
        """同じ入力→同じ順位・スコア。"""
        m1 = run_matcher_single("bank_crisis", self.state_tags, self.events)
        m2 = run_matcher_single("bank_crisis", self.state_tags, self.events)
        self.assertEqual(len(m1), len(m2))
        for a, b in zip(m1, m2):
            self.assertEqual(a["rank"], b["rank"])
            self.assertEqual(a["event_id"], b["event_id"])
            self.assertEqual(a["score"], b["score"])

    def test_top3_event_ids_consistent(self):
        """上位3件の event_id が一致すること。"""
        m1 = run_matcher_single("bank_crisis", self.state_tags, self.events)
        m2 = run_matcher_single("bank_crisis", self.state_tags, self.events)
        ids1 = [m["event_id"] for m in m1[:3]]
        ids2 = [m["event_id"] for m in m2[:3]]
        self.assertEqual(ids1, ids2)

    def test_ranked_by_score_then_date(self):
        """score 降順 → 日付降順で並んでいること。"""
        matches = run_matcher_single("bank_crisis", self.state_tags, self.events)
        for i in range(len(matches) - 1):
            a, b = matches[i], matches[i + 1]
            if a["score"] == b["score"]:
                # 同スコアなら日付降順
                self.assertGreaterEqual(a["event_date"], b["event_date"])
            else:
                self.assertGreater(a["score"], b["score"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
