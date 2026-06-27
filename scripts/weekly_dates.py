"""
Weekly Marketcast — ISO 8601 週処理

すべての週計算を datetime.date.fromisocalendar() に委譲する。
手動の曜日計算は行わない。
"""
from __future__ import annotations

import datetime
import re

_WEEK_ID_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")


def parse_week_id(week_id: str) -> tuple[int, int]:
    """week_id を検証して (year, week) を返す。不正なら ValueError。"""
    if not isinstance(week_id, str) or not _WEEK_ID_RE.match(week_id):
        raise ValueError(f"week_id のフォーマットが不正: {week_id!r}  (例: 2026-W25)")
    year_str, week_str = week_id.split("-W")
    year, week = int(year_str), int(week_str)
    # 存在しない W53 を拒否（例: 2025-W53 は存在しない）
    try:
        datetime.date.fromisocalendar(year, week, 1)
    except ValueError:
        raise ValueError(f"ISO週 {week_id} は実在しません")
    return year, week


def week_id_to_period(week_id: str) -> tuple[datetime.date, datetime.date]:
    """ISO週 → (period_start=月曜, period_end=金曜) を返す。"""
    year, week = parse_week_id(week_id)
    period_start = datetime.date.fromisocalendar(year, week, 1)  # 月曜
    period_end   = datetime.date.fromisocalendar(year, week, 5)  # 金曜
    return period_start, period_end


def prev_week_id(week_id: str) -> str:
    """前週の week_id を返す。年跨ぎを正しく処理する。"""
    year, week = parse_week_id(week_id)
    monday = datetime.date.fromisocalendar(year, week, 1)
    prev_monday = monday - datetime.timedelta(weeks=1)
    iso = prev_monday.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def date_to_week_id(d: datetime.date) -> str:
    """日付 → ISO週 week_id 変換。"""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_id_exists(week_id: str) -> bool:
    """week_id が実在する ISO 週かどうかを返す（例外を投げない）。"""
    try:
        parse_week_id(week_id)
        return True
    except ValueError:
        return False
