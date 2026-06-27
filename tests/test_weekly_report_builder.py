"""
test_weekly_report_builder.py — weekly_report_builder.py のユニットテスト

テスト対象:
  - build_asset_summaries(): 6資産サマリー構築
  - build_similar_events(): 類似局面変換
  - build_free_teaser(): free_teaser 構築
  - build_paid_body(): paid_body 構築
  - check_restricted_leak(): restricted leak check
  - check_free_teaser_leak(): free_teaser 有料情報混入チェック
  - check_forbidden_expressions(): 禁止表現チェック
  - compute_hash(): hash 生成
  - validate_hash_format(): hash 形式検証
  - validate_free_teaser() / validate_paid_body(): schema 検証
"""
import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).parent
FIXTURES  = TESTS_DIR / "fixtures" / "weekly"
SCRIPTS   = Path(__file__).parent.parent / "scripts"

sys.path.insert(0, str(SCRIPTS))

from weekly_report_builder import (
    build_asset_summaries,
    build_draft,
    build_free_teaser,
    build_paid_body,
    build_similar_events,
    build_title,
    check_forbidden_expressions,
    check_free_teaser_leak,
    check_restricted_leak,
    classify_oil,
    classify_vix,
    compute_hash,
    validate_free_teaser,
    validate_hash_format,
    validate_paid_body,
)


# ─── テストフィクスチャ ───────────────────────────────────────────────────────

def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def _valid_changes() -> dict:
    return _load_fixture("changes_valid.json")


def _valid_context() -> dict:
    return _load_fixture("context_public_fixture.json")


def _valid_matches() -> list[dict]:
    return _load_fixture("matcher_output_fixture.json")["matches"]


# ─── classify_vix / classify_oil テスト ──────────────────────────────────────

class TestClassify(unittest.TestCase):

    def test_classify_vix_calm(self):
        self.assertEqual(classify_vix(10.0), "calm")

    def test_classify_vix_elev(self):
        self.assertEqual(classify_vix(19.8), "elev")

    def test_classify_vix_stress(self):
        self.assertEqual(classify_vix(30.0), "stress")

    def test_classify_vix_panic(self):
        self.assertEqual(classify_vix(45.0), "panic")

    def test_classify_vix_none(self):
        self.assertIsNone(classify_vix(None))

    def test_classify_oil_lo(self):
        self.assertEqual(classify_oil(30.0), "lo")

    def test_classify_oil_mid(self):
        self.assertEqual(classify_oil(74.6), "mid")

    def test_classify_oil_hi(self):
        self.assertEqual(classify_oil(100.0), "hi")

    def test_classify_oil_none(self):
        self.assertIsNone(classify_oil(None))


# ─── build_asset_summaries テスト ────────────────────────────────────────────

class TestBuildAssetSummaries(unittest.TestCase):

    def setUp(self):
        self.changes = _valid_changes()
        self.context = _valid_context()

    def test_returns_6_assets(self):
        summaries, errors = build_asset_summaries(self.changes, self.context)
        self.assertEqual(len(summaries), 6, f"errors: {errors}")
        self.assertEqual(errors, [])

    def test_asset_order(self):
        summaries, _ = build_asset_summaries(self.changes, self.context)
        keys = [a["asset_key"] for a in summaries]
        self.assertEqual(keys, ["wti", "gold", "sp500", "ust10y", "usdjpy", "vix"])

    def test_gold_sp500_end_value_null(self):
        summaries, _ = build_asset_summaries(self.changes, self.context)
        for a in summaries:
            if a["asset_key"] in ("gold", "sp500"):
                self.assertIsNone(a["end_value"], f"{a['asset_key']} end_value should be null")
                self.assertTrue(a["restricted"], f"{a['asset_key']} restricted should be True")

    def test_other_assets_end_value_null(self):
        """W2-2 では全資産 end_value=null。"""
        summaries, _ = build_asset_summaries(self.changes, self.context)
        for a in summaries:
            self.assertIsNone(a["end_value"], f"{a['asset_key']} end_value should be null in W2-2")

    def test_ust10y_uses_pt_change(self):
        summaries, _ = build_asset_summaries(self.changes, self.context)
        ust10y = next(a for a in summaries if a["asset_key"] == "ust10y")
        self.assertIsNone(ust10y["pct_change"])
        self.assertIsNotNone(ust10y["pt_change"])

    def test_vix_level_class(self):
        """context の VIX 水準から level_class が設定されること（19.8 → elev）。"""
        summaries, _ = build_asset_summaries(self.changes, self.context)
        vix = next(a for a in summaries if a["asset_key"] == "vix")
        self.assertEqual(vix["level_class"], "elev")

    def test_wti_level_class(self):
        """context の WTI 水準から level_class が設定されること（74.6 → mid）。"""
        summaries, _ = build_asset_summaries(self.changes, self.context)
        wti = next(a for a in summaries if a["asset_key"] == "wti")
        self.assertEqual(wti["level_class"], "mid")

    def test_schema_validation(self):
        """asset_summaries が paid_body schema を通ること。"""
        summaries, _ = build_asset_summaries(self.changes, self.context)
        paid_body = {
            "summary": "テスト",
            "asset_summaries": summaries,
            "themes": [{"tag": "war", "label": "戦争", "summary": "x" * 1, "caveat": "y" * 1}],
            "similar_events": [
                {
                    "rank": 1, "event_id": "x", "event_name": "y", "event_date": "2022-01-01",
                    "score": 1, "matched_axes": [], "unmatched_axes": [],
                    "timelines": {}, "why_reaction": None, "key_insight": None,
                }
            ],
            "observation_points": ["a", "b", "c"],
            "disclaimer": "x",
        }
        errors = validate_paid_body(paid_body)
        self.assertEqual(errors, [], f"Schema errors: {errors}")


# ─── build_similar_events テスト ─────────────────────────────────────────────

class TestBuildSimilarEvents(unittest.TestCase):

    def test_basic_conversion(self):
        matches = _valid_matches()
        events, warns = build_similar_events(matches, max_events=3)
        self.assertEqual(len(events), 3)

    def test_has_required_fields(self):
        matches = _valid_matches()
        events, _ = build_similar_events(matches[:1], max_events=1)
        e = events[0]
        required = ["rank", "event_id", "event_name", "event_date", "score",
                    "matched_axes", "unmatched_axes", "timelines",
                    "why_reaction", "key_insight"]
        for field in required:
            self.assertIn(field, e, f"フィールド欠損: {field}")

    def test_timelines_direction_only(self):
        """timelines が方向文字列のみで raw 値を含まないこと。"""
        matches = _valid_matches()
        events, _ = build_similar_events(matches[:1])
        for asset_tl in events[0]["timelines"].values():
            self.assertIn(asset_tl["d1"], ("up", "down", "flat", "na"))
            self.assertIsInstance(asset_tl["mid_term_reversal"], bool)
            # raw 値がないこと
            self.assertNotIn("changes", asset_tl)
            self.assertNotIn("changes_pt", asset_tl)
            self.assertNotIn("value", asset_tl)

    def test_warn_when_less_than_3(self):
        matches = _valid_matches()[:1]
        _, warns = build_similar_events(matches, max_events=3)
        self.assertTrue(any("3件未満" in w for w in warns))

    def test_why_reaction_null_generates_warn(self):
        matches = _valid_matches()
        _, warns = build_similar_events(matches[:1])
        self.assertTrue(any("why_reaction" in w for w in warns))


# ─── hash テスト ─────────────────────────────────────────────────────────────

class TestComputeHash(unittest.TestCase):

    def test_deterministic(self):
        obj = {"a": 1, "b": "テスト"}
        self.assertEqual(compute_hash(obj), compute_hash(obj))

    def test_key_order_invariant(self):
        obj1 = {"a": 1, "b": "x"}
        obj2 = {"b": "x", "a": 1}
        self.assertEqual(compute_hash(obj1), compute_hash(obj2))

    def test_content_change_changes_hash(self):
        h1 = compute_hash({"value": 1})
        h2 = compute_hash({"value": 2})
        self.assertNotEqual(h1, h2)

    def test_lowercase_hex(self):
        h = compute_hash({"x": "y"})
        self.assertEqual(h, h.lower())
        self.assertTrue(all(c in "0123456789abcdef" for c in h))

    def test_64_chars(self):
        h = compute_hash({"x": "y"})
        self.assertEqual(len(h), 64)

    def test_validate_hash_format_ok(self):
        h = compute_hash({"x": 1})
        errors = validate_hash_format(h)
        self.assertEqual(errors, [])

    def test_validate_hash_format_uppercase(self):
        errors = validate_hash_format("A" * 64)
        self.assertTrue(len(errors) > 0)

    def test_validate_hash_format_wrong_length(self):
        errors = validate_hash_format("abc123")
        self.assertTrue(len(errors) > 0)


# ─── leak check テスト ───────────────────────────────────────────────────────

class TestLeakCheck(unittest.TestCase):

    def test_clean_dict(self):
        errors = check_restricted_leak({"asset_key": "wti", "direction": "up"})
        self.assertEqual(errors, [])

    def test_detects_value_key(self):
        errors = check_restricted_leak({"value": 123.45})
        self.assertTrue(any("value" in e for e in errors))

    def test_detects_current_value(self):
        errors = check_restricted_leak({"current_value": 100.0})
        self.assertTrue(len(errors) > 0)

    def test_detects_nested(self):
        errors = check_restricted_leak({"asset": {"price": 50.0}})
        self.assertTrue(len(errors) > 0)

    def test_detects_in_list(self):
        errors = check_restricted_leak([{"value": 1}, {"ok": "yes"}])
        self.assertTrue(len(errors) > 0)

    def test_end_value_null_is_ok(self):
        """end_value キーは許可（値が null なら OK）。"""
        errors = check_restricted_leak({"end_value": None})
        self.assertEqual(errors, [])

    def test_end_value_non_null_restricted_detected_by_schema(self):
        """gold の end_value != null は schema 検証で検出される。"""
        paid_body_like = {
            "summary": "test",
            "asset_summaries": [
                {"asset_key": "gold", "label": "金", "restricted": True, "direction": "up",
                 "pct_change": 1.0, "pt_change": None, "level_class": None, "end_value": 2000.0}
            ],
            "themes": [{"tag": "war", "label": "戦争", "summary": "x", "caveat": "y"}],
            "similar_events": [
                {"rank": 1, "event_id": "x", "event_name": "y", "event_date": "2022-01-01",
                 "score": 1, "matched_axes": [], "unmatched_axes": [],
                 "timelines": {}, "why_reaction": None, "key_insight": None}
            ],
            "observation_points": ["a", "b", "c"],
            "disclaimer": "x",
        }
        errors = validate_paid_body(paid_body_like)
        self.assertTrue(len(errors) > 0, "gold の non-null end_value は schema で拒否されるべき")


class TestFreeLeakCheck(unittest.TestCase):

    def test_score_in_free_detected(self):
        errors = check_free_teaser_leak({"week_id": "2026-W25", "score": 3.0})
        self.assertTrue(any("score" in e for e in errors))

    def test_timelines_in_free_detected(self):
        errors = check_free_teaser_leak({"timelines": {"wti": {}}})
        self.assertTrue(len(errors) > 0)

    def test_end_value_in_free_detected(self):
        errors = check_free_teaser_leak({"asset": {"end_value": 100.0}})
        self.assertTrue(len(errors) > 0)

    def test_clean_free_teaser(self):
        free = {
            "week_id": "2026-W25",
            "title": "Weekly Marketcast",
            "period_start": "2026-06-15",
            "period_end": "2026-06-19",
            "env_label": "VIX警戒",
            "teaser_summary": "要約",
            "featured_theme": None,
            "top_match_preview": None,
            "disclaimer": "免責",
        }
        errors = check_free_teaser_leak(free)
        self.assertEqual(errors, [])


# ─── 禁止表現チェックテスト ───────────────────────────────────────────────────

class TestForbiddenExpressions(unittest.TestCase):

    def test_detects_forbidden(self):
        errors = check_forbidden_expressions(["これは買い時かもしれません"])
        self.assertTrue(len(errors) > 0)

    def test_detects_shousetsu(self):
        errors = check_forbidden_expressions(["上昇するだろう"])
        self.assertTrue(len(errors) > 0)

    def test_clean_text(self):
        errors = check_forbidden_expressions(["VIXの水準が前週から変化するかを確認する"])
        self.assertEqual(errors, [])

    def test_multiple_texts(self):
        texts = ["安全なテキスト", "投資妙味のある銘柄", "別の安全テキスト"]
        errors = check_forbidden_expressions(texts)
        self.assertTrue(any("投資妙味" in e for e in errors))


# ─── Schema 検証テスト ───────────────────────────────────────────────────────

class TestSchemaValidation(unittest.TestCase):

    def test_valid_free_teaser(self):
        free = _load_fixture("free_teaser_valid.json")
        errors = validate_free_teaser(free)
        self.assertEqual(errors, [])

    def test_valid_paid_body(self):
        paid = _load_fixture("paid_body_valid.json")
        errors = validate_paid_body(paid)
        self.assertEqual(errors, [])

    def test_invalid_free_teaser_score(self):
        """free_teaser に score があると additionalProperties で失敗。"""
        free = _load_fixture("free_teaser_invalid_score.json")
        errors = validate_free_teaser(free)
        self.assertTrue(len(errors) > 0)

    def test_invalid_paid_body_restricted_value(self):
        """gold の end_value が non-null だと失敗。"""
        paid = _load_fixture("paid_body_invalid_restricted_value.json")
        errors = validate_paid_body(paid)
        self.assertTrue(len(errors) > 0)


# ─── build_title テスト ──────────────────────────────────────────────────────

class TestBuildTitle(unittest.TestCase):

    def test_format(self):
        title = build_title("2026-W25", "2026-06-15", "2026-06-19")
        self.assertIn("2026", title)
        self.assertIn("25", title)
        self.assertIn("6/15", title)
        self.assertIn("6/19", title)

    def test_within_100_chars(self):
        title = build_title("2026-W25", "2026-06-15", "2026-06-19")
        self.assertLessEqual(len(title), 100)


# ─── build_draft 統合テスト ───────────────────────────────────────────────────

class TestBuildDraft(unittest.TestCase):

    def setUp(self):
        self.changes = _valid_changes()
        self.context = _valid_context()
        self.matches = _valid_matches()

    def test_draft_structure(self):
        """draft が必須フィールドを持つこと。"""
        draft, _, _ = build_draft(self.changes, self.matches, self.context)
        required = ["week_id", "revision", "generated_at", "free_teaser",
                    "paid_body", "teaser_hash", "paid_body_hash", "warnings", "hard_errors"]
        for field in required:
            self.assertIn(field, draft, f"フィールド欠損: {field}")

    def test_no_restricted_leak(self):
        """draft に forbidden キーが含まれないこと。"""
        draft, _, _ = build_draft(self.changes, self.matches, self.context)
        errors = check_restricted_leak(draft)
        self.assertEqual(errors, [], f"Leak errors: {errors}")

    def test_hash_valid(self):
        draft, _, hard = build_draft(self.changes, self.matches, self.context)
        if not hard:
            self.assertEqual(len(draft["teaser_hash"]), 64)
            self.assertEqual(len(draft["paid_body_hash"]), 64)

    def test_free_teaser_no_score(self):
        """free_teaser に score が含まれないこと。"""
        draft, _, _ = build_draft(self.changes, self.matches, self.context)
        errors = check_free_teaser_leak(draft["free_teaser"])
        self.assertEqual(errors, [], f"Free teaser leak: {errors}")

    def test_asset_summaries_count(self):
        """paid_body.asset_summaries が6件であること。"""
        draft, _, _ = build_draft(self.changes, self.matches, self.context)
        self.assertEqual(len(draft["paid_body"]["asset_summaries"]), 6)

    def test_schemas_pass(self):
        """free_teaser と paid_body が schema を通ること。"""
        draft, _, hard = build_draft(self.changes, self.matches, self.context)
        if not hard:
            self.assertEqual(validate_free_teaser(draft["free_teaser"]), [])
            self.assertEqual(validate_paid_body(draft["paid_body"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
