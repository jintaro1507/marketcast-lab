#!/usr/bin/env python3
"""
calculate_weekly_changes.py — 週次差分計算

使用例:
  python scripts/calculate_weekly_changes.py --week-id 2026-W26
  python scripts/calculate_weekly_changes.py --week-id 2026-W26 --out changes.json

DB から当週・前週のスナップを取得し、週次変化を計算する。
出力 JSON には restricted 生値（current_value / previous_value）を含まない。

終了コード:
  0: 成功（WARN があっても 0）
  1: HARD エラー
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from weekly_config import ASSET_CONFIGS, get_asset_config, is_restricted
from weekly_dates import week_id_to_period, prev_week_id
from weekly_db import WeeklyDB, WeeklyDBError
from weekly_secrets import load_secrets, SecretsError, mask_secret


# ── 差分計算 ─────────────────────────────────────────────────────────

def compute_change(
    cfg: dict,
    current_value: float | None,
    previous_value: float | None,
) -> tuple[float | None, float | None, str]:
    """
    (pct_change, pt_change, direction) を返す。
    current_value / previous_value は出力 JSON に含めない。
    """
    if current_value is None or previous_value is None:
        return None, None, "na"

    change_type = cfg["change_type"]

    if change_type == "pt":
        pt = round(current_value - previous_value, 4)
        pct = None
        change = pt
    else:
        if previous_value == 0:
            return None, None, "na"
        pct = round((current_value - previous_value) / abs(previous_value) * 100, 2)
        pt  = None
        change = pct

    flat_low  = cfg["flat_low"]
    flat_high = cfg["flat_high"]

    # change >= flat_high → up
    # change <= flat_low  → down
    # それ以外            → flat
    if change >= flat_high:
        direction = "up"
    elif change <= flat_low:
        direction = "down"
    else:
        direction = "flat"

    return pct, pt, direction


# ── メイン ────────────────────────────────────────────────────────────

def build_changes(
    week_id: str,
    db: WeeklyDB,
    secrets: dict,
) -> dict:
    """差分 JSON を構築して返す。hard_errors がある場合も JSON を返す。"""
    hard: list[str] = []
    warn: list[str]  = []

    # week_id → period
    try:
        period_start, period_end = week_id_to_period(week_id)
    except ValueError as e:
        hard.append(f"week_id 不正: {e}")
        return _error_result(week_id, hard, warn)

    prev_wid = prev_week_id(week_id)

    # 当週スナップ取得
    try:
        curr_rows = db.get_snapshots(week_id)
    except WeeklyDBError as e:
        msg = mask_secret(str(e), secrets)
        hard.append(f"当週スナップ取得失敗: {msg}")
        return _error_result(week_id, hard, warn, period_start, period_end, prev_wid)

    if not curr_rows:
        hard.append(f"当週スナップが存在しません: {week_id}")
        return _error_result(week_id, hard, warn, period_start, period_end, prev_wid)

    # 前週スナップ取得
    try:
        prev_rows = db.get_snapshots(prev_wid)
    except WeeklyDBError as e:
        msg = mask_secret(str(e), secrets)
        hard.append(f"前週スナップ取得失敗: {msg}")
        return _error_result(week_id, hard, warn, period_start, period_end, prev_wid)

    if not prev_rows:
        hard.append(f"前週スナップが存在しません: {prev_wid}")
        return _error_result(week_id, hard, warn, period_start, period_end, prev_wid)

    # DB のスナップからは value フィールドが返ってこないことに注意:
    # get_snapshots() は SELECT に value を含めない。
    # 差分計算には value が必要なため、value を含む専用クエリを使う。
    curr_map = _rows_with_value(db, week_id)
    prev_map = _rows_with_value(db, prev_wid)

    # 件数確認
    if len(curr_map) != 6:
        hard.append(f"当週スナップ件数が6件でない: {len(curr_map)}件")
    if len(prev_map) != 6:
        hard.append(f"前週スナップ件数が6件でない: {len(prev_map)}件")

    if hard:
        return _error_result(week_id, hard, warn, period_start, period_end, prev_wid)

    # 前週 seeded 警告
    for r in prev_map.values():
        if r.get("seeded"):
            warn.append(f"前週 {prev_wid} の {r['asset_key']} は seeded スナップです")

    # 当週の status チェック
    non_ok_curr = [k for k, v in curr_map.items() if v.get("status") != "ok"]
    if len(non_ok_curr) >= 2:
        hard.append(f"当週: status!=ok が2件以上: {non_ok_curr}")
    elif len(non_ok_curr) == 1:
        warn.append(f"当週: status!=ok が1件: {non_ok_curr[0]}")

    if hard:
        return _error_result(week_id, hard, warn, period_start, period_end, prev_wid)

    # 資産ごとの差分計算
    assets_out: list[dict] = []
    for cfg in ASSET_CONFIGS:
        key = cfg["asset_key"]
        curr = curr_map.get(key, {})
        prev = prev_map.get(key, {})

        curr_val = curr.get("value")
        prev_val = prev.get("value")
        curr_status = curr.get("status", "error")
        asset_warn: list[str] = []

        if curr_status != "ok" or curr_val is None:
            pct, pt, direction = None, None, "na"
        elif prev_val is None:
            pct, pt, direction = None, None, "na"
            asset_warn.append("前週値なし")
        else:
            pct, pt, direction = compute_change(cfg, curr_val, prev_val)
            if direction == "na":
                hard.append(f"{key}: 前週値が0のため変化率計算不可")

        if prev.get("seeded"):
            asset_warn.append("前週値は seeded スナップ")

        assets_out.append({
            "asset_key":    key,
            "restricted":   is_restricted(key),
            "status":       curr_status,
            "as_of":        curr.get("as_of"),
            "previous_as_of": prev.get("as_of"),
            "pct_change":   pct,
            "pt_change":    pt,
            "direction":    direction,
            "warn":         asset_warn,
        })

    return {
        "week_id":          week_id,
        "previous_week_id": prev_wid,
        "period_start":     str(period_start),
        "period_end":       str(period_end),
        "assets":           assets_out,
        "hard_errors":      hard,
        "warnings":         warn,
        "generated_at":     datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def _rows_with_value(db: WeeklyDB, week_id: str) -> dict[str, dict]:
    """value を含むスナップを取得する（差分計算用）。"""
    _, rows = db._request(
        "GET",
        f"/weekly_asset_snapshots?week_id=eq.{week_id}"
        "&select=asset_key,status,value,as_of,restricted,seeded",
    )
    return {r["asset_key"]: r for r in (rows or [])}


def _error_result(
    week_id: str,
    hard: list[str],
    warn: list[str],
    period_start=None,
    period_end=None,
    prev_wid: str | None = None,
) -> dict:
    return {
        "week_id":          week_id,
        "previous_week_id": prev_wid or "",
        "period_start":     str(period_start) if period_start else "",
        "period_end":       str(period_end) if period_end else "",
        "assets":           [],
        "hard_errors":      hard,
        "warnings":         warn,
        "generated_at":     datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="週次差分計算")
    parser.add_argument("--week-id", required=True, help="例: 2026-W26")
    parser.add_argument("--out", default=None, help="出力 JSON ファイルパス（省略時は stdout）")
    args = parser.parse_args()

    try:
        secrets = load_secrets()
    except SecretsError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    db = WeeklyDB(secrets["SUPABASE_URL"], secrets["SUPABASE_SERVICE_ROLE_KEY"])

    result = build_changes(args.week_id, db, secrets)

    # restricted 生値が混入していないか最終確認
    _assert_no_raw_values(result)

    out_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.out:
        Path(args.out).write_text(out_json, encoding="utf-8")
        print(f"出力: {args.out}")
    else:
        print(out_json)

    if result["hard_errors"]:
        print(f"\n[HARD] {len(result['hard_errors'])} 件のエラーがあります。", file=sys.stderr)
        sys.exit(1)

    if result["warnings"]:
        print(f"\n[WARN] {len(result['warnings'])} 件の警告があります。", file=sys.stderr)

    sys.exit(0)


def _assert_no_raw_values(result: dict) -> None:
    """出力 JSON に current_value / previous_value が含まれていないことを確認。"""
    forbidden = {"current_value", "previous_value", "value"}
    for asset in result.get("assets", []):
        found = forbidden & set(asset.keys())
        if found:
            raise RuntimeError(
                f"[HARD] 差分出力に禁止フィールドが含まれています: {found}\n"
                "これはバグです。出力を停止します。"
            )


if __name__ == "__main__":
    main()
