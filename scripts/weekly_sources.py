"""
Weekly Marketcast — 週次データ取得

fetch_and_build.py の取得関数を再利用し、ISO週のperiod内に
絞り込んだ観測値を返す。

月曜〜金曜のみ対象。土日・対象週外は除外。
null / 欠損値は除外済み（fetch_and_build.py 側で処理済み）。
"""
from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

# scripts/ をパスへ追加（fetch_and_build をインポートするため）
_SCRIPTS_DIR = str(Path(__file__).parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _get_fab(fred_api_key: str) -> tuple:
    """
    fetch_and_build をインポートし、FRED_API_KEY をパッチして返す。
    Returns (module, original_key) — 呼び出し元で finally 節に元のキーを復元すること。
    """
    import fetch_and_build as _fab  # noqa: PLC0415
    orig = _fab.FRED_API_KEY
    _fab.FRED_API_KEY = fred_api_key
    return _fab, orig


def classify_stooq_error(exc: Exception) -> str:
    """Stooq取得例外を安全な固定コードに分類する。例外本文・response断片を出力しない。"""
    from urllib.error import HTTPError, URLError
    if isinstance(exc, HTTPError):
        return f"stooq_http_error:{exc.code}"
    if isinstance(exc, URLError):
        return "stooq_connection_error"
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        if "HTML応答" in msg:
            return "stooq_html_response"
        if "CSVヘッダなし" in msg:
            return "stooq_invalid_csv_header"
        if "データ行が空" in msg:
            return "stooq_empty_data_rows"
    return "stooq_unknown_error"


def fetch_week_observations(
    asset_config: dict,
    fred_api_key: str,
    period_start: datetime.date,
    period_end: datetime.date,
) -> tuple[list[tuple[str, float]], str]:
    """
    指定 ISO 週の観測値を取得する。

    Returns:
        (observations, source_used)
        observations: [(date_str, float), ...] — 対象週内、月〜金のみ、null 除外済み
        source_used : "fred_DCOILWTICO" / "stooq_gld.us" / "yahoo_GLD" 等
    """
    today = datetime.date.today()
    days_needed = max((today - period_start).days + 30, 90)

    fab, orig_key = _get_fab(fred_api_key)
    source = asset_config["source"]
    source_used: str

    try:
        if source == "stooq":
            try:
                raw = fab.fetch_stooq_series(asset_config["stooq_id"], days_needed)
                source_used = f"stooq_{asset_config['stooq_id']}"
            except Exception as e:
                print(f"  [Stooq→Yahoo フォールバック] {asset_config['stooq_id']}: {classify_stooq_error(e)}")
                raw = fab.fetch_yahoo_series(asset_config["yahoo_symbol"], days_needed)
                source_used = f"yahoo_{asset_config['yahoo_symbol']}"
        elif source == "fred":
            raw = fab.fetch_fred_series(asset_config["fred_id"], days_needed)
            source_used = f"fred_{asset_config['fred_id']}"
        else:
            raise ValueError(f"Unknown source: {source!r}")
    finally:
        fab.FRED_API_KEY = orig_key

    # 対象週内（月〜金）に絞り込む
    week_obs: list[tuple[str, float]] = []
    for date_str, value in raw:
        d = datetime.date.fromisoformat(date_str)
        if period_start <= d <= period_end and d.weekday() <= 4:  # 0=月 4=金
            week_obs.append((date_str, value))

    return week_obs, source_used


def select_last_valid(
    observations: list[tuple[str, float]],
) -> tuple[str, float] | None:
    """
    観測値リストから最後の有効値を選択する。
    observations はすでに対象週・月〜金・null除外済みのリスト。
    """
    if not observations:
        return None
    # リストは昇順（fetch_and_build が sort_order="asc" で取得）
    return observations[-1]
