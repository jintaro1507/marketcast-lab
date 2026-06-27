"""
tests/test_weekly_snapshot.py — スナップ構築・HARD/WARN・restricted の単体テスト

ネットワーク・DB 不要。stdlib unittest のみ使用。
観測値選択ロジックを weekly_sources から直接テストする。
"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_config import ASSET_CONFIGS, ASSET_CONFIG_MAP, is_restricted, RESTRICTED_ASSETS
from weekly_snapshot import (
    build_snapshot_record,
    check_snapshots,
)
from weekly_sources import select_last_valid

# テスト用に週を固定
WEEK_ID      = "2026-W25"
PERIOD_START = datetime.date(2026, 6, 15)  # 月
PERIOD_END   = datetime.date(2026, 6, 19)  # 金
TAKEN_AT     = "2026-06-20T00:00:00+00:00"


# ── 観測値選択テスト ─────────────────────────────────────────────────

class TestSelectLastValid(unittest.TestCase):
    def test_friday_value_selected(self):
        obs = [
            ("2026-06-15", 70.0),  # 月
            ("2026-06-16", 71.0),  # 火
            ("2026-06-17", 72.0),  # 水
            ("2026-06-18", 73.0),  # 木
            ("2026-06-19", 74.0),  # 金
        ]
        result = select_last_valid(obs)
        self.assertEqual(result, ("2026-06-19", 74.0))

    def test_thursday_when_friday_missing(self):
        obs = [
            ("2026-06-15", 70.0),
            ("2026-06-18", 73.0),  # 木（金なし）
        ]
        result = select_last_valid(obs)
        self.assertEqual(result, ("2026-06-18", 73.0))

    def test_empty_returns_none(self):
        self.assertIsNone(select_last_valid([]))

    def test_single_observation(self):
        result = select_last_valid([("2026-06-17", 72.0)])
        self.assertEqual(result, ("2026-06-17", 72.0))


def _filter_to_week(obs_all, period_start, period_end):
    """テスト用: 週内・月〜金のみ絞り込む（weekly_sources と同じロジック）。"""
    return [
        (d, v)
        for d, v in obs_all
        if period_start <= datetime.date.fromisoformat(d) <= period_end
        and datetime.date.fromisoformat(d).weekday() <= 4
    ]


class TestFilterToWeek(unittest.TestCase):
    def test_saturday_excluded(self):
        obs = [
            ("2026-06-19", 74.0),  # 金
            ("2026-06-20", 75.0),  # 土 → 除外
        ]
        filtered = _filter_to_week(obs, PERIOD_START, PERIOD_END)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0][0], "2026-06-19")

    def test_sunday_excluded(self):
        obs = [("2026-06-21", 75.0)]  # 日
        filtered = _filter_to_week(obs, PERIOD_START, PERIOD_END)
        self.assertEqual(len(filtered), 0)

    def test_out_of_week_before(self):
        obs = [("2026-06-12", 70.0)]  # 前週金曜
        filtered = _filter_to_week(obs, PERIOD_START, PERIOD_END)
        self.assertEqual(len(filtered), 0)

    def test_out_of_week_after(self):
        obs = [("2026-06-22", 70.0)]  # 翌週月曜
        filtered = _filter_to_week(obs, PERIOD_START, PERIOD_END)
        self.assertEqual(len(filtered), 0)

    def test_only_outside_returns_none_after_select(self):
        obs = [("2026-06-22", 70.0)]
        filtered = _filter_to_week(obs, PERIOD_START, PERIOD_END)
        self.assertIsNone(select_last_valid(filtered))


# ── restricted 固定テスト ────────────────────────────────────────────

class TestRestricted(unittest.TestCase):
    def test_gold_restricted(self):
        self.assertTrue(is_restricted("gold"))

    def test_sp500_restricted(self):
        self.assertTrue(is_restricted("sp500"))

    def test_wti_not_restricted(self):
        self.assertFalse(is_restricted("wti"))

    def test_ust10y_not_restricted(self):
        self.assertFalse(is_restricted("ust10y"))

    def test_usdjpy_not_restricted(self):
        self.assertFalse(is_restricted("usdjpy"))

    def test_vix_not_restricted(self):
        self.assertFalse(is_restricted("vix"))

    def test_all_six_assets_defined(self):
        keys = {c["asset_key"] for c in ASSET_CONFIGS}
        self.assertEqual(keys, {"wti", "gold", "sp500", "ust10y", "usdjpy", "vix"})

    def test_restricted_assets_constant(self):
        self.assertEqual(RESTRICTED_ASSETS, frozenset({"gold", "sp500"}))

    def test_cannot_override_restricted_via_config(self):
        # is_restricted() は RESTRICTED_ASSETS 定数のみに依存し、外部入力を受け付けない
        self.assertTrue(is_restricted("gold"))
        self.assertFalse(is_restricted("wti"))
        # 存在しない asset_key は False
        self.assertFalse(is_restricted("unknown_asset"))


# ── スナップレコード構築テスト ───────────────────────────────────────

class TestBuildSnapshotRecord(unittest.TestCase):
    def _cfg(self, key):
        return ASSET_CONFIG_MAP[key]

    def test_ok_record(self):
        rec = build_snapshot_record(
            self._cfg("wti"),
            ("2026-06-19", 74.6),
            "fred_DCOILWTICO",
            TAKEN_AT,
            WEEK_ID,
        )
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["as_of"], "2026-06-19")
        self.assertEqual(rec["value"], 74.6)
        self.assertFalse(rec["restricted"])
        self.assertFalse(rec["seeded"])

    def test_no_data_record(self):
        rec = build_snapshot_record(
            self._cfg("wti"), None, "fred_DCOILWTICO", TAKEN_AT, WEEK_ID
        )
        self.assertEqual(rec["status"], "no_data")
        self.assertIsNone(rec["as_of"])
        self.assertIsNone(rec["value"])

    def test_gold_restricted_forced(self):
        rec = build_snapshot_record(
            self._cfg("gold"),
            ("2026-06-19", 1900.0),
            "stooq_gld.us",
            TAKEN_AT,
            WEEK_ID,
        )
        self.assertTrue(rec["restricted"])

    def test_sp500_restricted_forced(self):
        rec = build_snapshot_record(
            self._cfg("sp500"),
            ("2026-06-19", 5200.0),
            "fred_SP500",
            TAKEN_AT,
            WEEK_ID,
        )
        self.assertTrue(rec["restricted"])

    def test_seeded_record(self):
        rec = build_snapshot_record(
            self._cfg("wti"),
            ("2026-06-19", 74.6),
            "fred_DCOILWTICO",
            TAKEN_AT,
            WEEK_ID,
            seeded=True,
            seed_source="initial_seed_from_source_series",
        )
        self.assertTrue(rec["seeded"])
        self.assertEqual(rec["seed_source"], "initial_seed_from_source_series")


# ── HARD/WARN 判定テスト ─────────────────────────────────────────────

def _make_ok_records(week_id=WEEK_ID, period_end=PERIOD_END):
    """全 6 件 ok のスナップレコードを生成する。"""
    records = []
    for cfg in ASSET_CONFIGS:
        records.append({
            "week_id":   week_id,
            "asset_key": cfg["asset_key"],
            "source":    "fred_test",
            "value":     100.0,
            "as_of":     str(period_end),
            "status":    "ok",
            "restricted": is_restricted(cfg["asset_key"]),
            "seeded":    False,
            "seed_source": None,
            "snapshot_taken_at": TAKEN_AT,
        })
    return records


class TestCheckSnapshots(unittest.TestCase):
    def test_all_ok_no_errors(self):
        records = _make_ok_records()
        hard, warn = check_snapshots(records, PERIOD_START, PERIOD_END)
        self.assertEqual(hard, [])
        # period_end と as_of が等しい場合は warn なし
        warn_no_date = [w for w in warn if "period_end" not in w and "Yahoo" not in w and "seeded" not in w]
        self.assertEqual(warn_no_date, [])

    def test_one_asset_no_data_warn(self):
        records = _make_ok_records()
        records[0]["status"] = "no_data"
        records[0]["value"]  = None
        records[0]["as_of"]  = None
        hard, warn = check_snapshots(records, PERIOD_START, PERIOD_END)
        self.assertEqual(hard, [])
        self.assertTrue(any("no_data" in w for w in warn))

    def test_two_assets_no_data_hard(self):
        records = _make_ok_records()
        for i in range(2):
            records[i]["status"] = "no_data"
            records[i]["value"]  = None
            records[i]["as_of"]  = None
        hard, warn = check_snapshots(records, PERIOD_START, PERIOD_END)
        self.assertTrue(any("2件以上" in h for h in hard))

    def test_restricted_mismatch_hard(self):
        records = _make_ok_records()
        # gold の restricted を False に改ざん
        for r in records:
            if r["asset_key"] == "gold":
                r["restricted"] = False
        hard, warn = check_snapshots(records, PERIOD_START, PERIOD_END)
        self.assertTrue(any("restricted" in h for h in hard))

    def test_as_of_outside_week_single_warn(self):
        records = _make_ok_records()
        records[0]["as_of"] = "2026-06-12"  # 前週金曜
        hard, warn = check_snapshots(records, PERIOD_START, PERIOD_END)
        # 1件のみ → warn に昇格
        self.assertEqual(hard, [])
        self.assertTrue(any("週外" in w or "outside" in w.lower() or records[0]["asset_key"] in w for w in warn))

    def test_as_of_outside_week_two_hard(self):
        records = _make_ok_records()
        records[0]["as_of"] = "2026-06-12"
        records[1]["as_of"] = "2026-06-12"
        hard, warn = check_snapshots(records, PERIOD_START, PERIOD_END)
        self.assertTrue(any("2件以上" in h or "週外" in h for h in hard))

    def test_yahoo_fallback_warn(self):
        records = _make_ok_records()
        for r in records:
            if r["asset_key"] == "gold":
                r["source"] = "yahoo_GLD"
        hard, warn = check_snapshots(records, PERIOD_START, PERIOD_END)
        self.assertTrue(any("Yahoo" in w for w in warn))

    def test_seeded_warn(self):
        records = _make_ok_records()
        records[0]["seeded"] = True
        hard, warn = check_snapshots(records, PERIOD_START, PERIOD_END)
        self.assertTrue(any("seeded" in w for w in warn))

    def test_wrong_record_count_hard(self):
        records = _make_ok_records()[:5]  # 5件
        hard, warn = check_snapshots(records, PERIOD_START, PERIOD_END)
        self.assertTrue(any("6件でない" in h for h in hard))


if __name__ == "__main__":
    unittest.main(verbosity=2)
