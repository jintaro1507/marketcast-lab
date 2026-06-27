"""
tests/test_weekly_dates.py — ISO 週処理の単体テスト

ネットワーク・DB 不要。stdlib unittest のみ使用。
"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from weekly_dates import (
    date_to_week_id,
    parse_week_id,
    prev_week_id,
    week_id_exists,
    week_id_to_period,
)


class TestParseWeekId(unittest.TestCase):
    def test_normal_week(self):
        year, week = parse_week_id("2026-W25")
        self.assertEqual(year, 2026)
        self.assertEqual(week, 25)

    def test_w01(self):
        year, week = parse_week_id("2026-W01")
        self.assertEqual(week, 1)

    def test_w52(self):
        year, week = parse_week_id("2026-W52")
        self.assertEqual(week, 52)

    def test_valid_w53_2020(self):
        # 2020 年は W53 が存在する
        year, week = parse_week_id("2020-W53")
        self.assertEqual(week, 53)

    def test_invalid_w53_2023(self):
        # 2023 年は W53 が存在しない
        with self.assertRaises(ValueError):
            parse_week_id("2023-W53")

    def test_invalid_format_no_w(self):
        with self.assertRaises(ValueError):
            parse_week_id("2026-25")

    def test_invalid_format_wrong_prefix(self):
        with self.assertRaises(ValueError):
            parse_week_id("2026W25")

    def test_invalid_week_00(self):
        with self.assertRaises(ValueError):
            parse_week_id("2026-W00")

    def test_invalid_week_54(self):
        with self.assertRaises(ValueError):
            parse_week_id("2026-W54")

    def test_none_input(self):
        with self.assertRaises((ValueError, TypeError, AttributeError)):
            parse_week_id(None)

    def test_empty_string(self):
        with self.assertRaises(ValueError):
            parse_week_id("")


class TestWeekIdToPeriod(unittest.TestCase):
    def test_w25_2026(self):
        start, end = week_id_to_period("2026-W25")
        self.assertEqual(start, datetime.date(2026, 6, 15))
        self.assertEqual(end,   datetime.date(2026, 6, 19))
        self.assertEqual(start.weekday(), 0)  # 月曜
        self.assertEqual(end.weekday(),   4)  # 金曜

    def test_w26_2026(self):
        start, end = week_id_to_period("2026-W26")
        self.assertEqual(start, datetime.date(2026, 6, 22))
        self.assertEqual(end,   datetime.date(2026, 6, 26))

    def test_w01_2026(self):
        start, end = week_id_to_period("2026-W01")
        self.assertEqual(start, datetime.date(2025, 12, 29))
        self.assertEqual(end,   datetime.date(2026, 1,  2))

    def test_period_end_is_always_friday(self):
        for week in range(1, 53):
            wid = f"2026-W{week:02d}"
            if not week_id_exists(wid):
                continue
            _, end = week_id_to_period(wid)
            self.assertEqual(end.weekday(), 4, f"{wid}: period_end is not Friday")

    def test_period_start_is_always_monday(self):
        for week in range(1, 53):
            wid = f"2026-W{week:02d}"
            if not week_id_exists(wid):
                continue
            start, _ = week_id_to_period(wid)
            self.assertEqual(start.weekday(), 0, f"{wid}: period_start is not Monday")


class TestPrevWeekId(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(prev_week_id("2026-W25"), "2026-W24")

    def test_w01_to_prev_year(self):
        # 2026-W01 の前週は 2025-W52
        result = prev_week_id("2026-W01")
        self.assertEqual(result, "2025-W52")

    def test_cross_year_w53(self):
        # 2021-W01 の前週は 2020-W53（2020 年は W53 が存在する）
        result = prev_week_id("2021-W01")
        self.assertEqual(result, "2020-W53")

    def test_mid_year(self):
        self.assertEqual(prev_week_id("2026-W26"), "2026-W25")

    def test_w52_to_w51(self):
        self.assertEqual(prev_week_id("2026-W52"), "2026-W51")


class TestDateToWeekId(unittest.TestCase):
    def test_monday(self):
        self.assertEqual(date_to_week_id(datetime.date(2026, 6, 15)), "2026-W25")

    def test_friday(self):
        self.assertEqual(date_to_week_id(datetime.date(2026, 6, 19)), "2026-W25")

    def test_sunday_belongs_to_prev_week(self):
        # ISO 週では日曜は前週に属する
        # 2026-06-14 (日) は W24
        self.assertEqual(date_to_week_id(datetime.date(2026, 6, 14)), "2026-W24")

    def test_year_start_jan1(self):
        # 2026-01-01 (木) は 2026-W01
        self.assertEqual(date_to_week_id(datetime.date(2026, 1, 1)), "2026-W01")


class TestWeekIdExists(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(week_id_exists("2026-W25"))

    def test_invalid_w53(self):
        # 2023 年は W53 が存在しない
        self.assertFalse(week_id_exists("2023-W53"))

    def test_valid_w53_2020(self):
        self.assertTrue(week_id_exists("2020-W53"))

    def test_invalid_format(self):
        self.assertFalse(week_id_exists("bad"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
