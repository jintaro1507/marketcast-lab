#!/usr/bin/env python3
"""
save_weekly_snapshot.py — 週次スナップショット取得・保存

使用例:
  python scripts/save_weekly_snapshot.py --week-id 2026-W26
  python scripts/save_weekly_snapshot.py --week-id 2026-W26 --dry-run
  python scripts/save_weekly_snapshot.py --week-id 2026-W26 --production

--dry-run:    DB 書き込みを行わずプレビューのみ表示する。
--production: 本番 Supabase への書き込みを許可する
              （省略時はローカル/非本番のみ許可）。

秘密情報の読み込み元: ~/.config/marketcast-lab/.env
"""

import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from weekly_config import ASSET_CONFIGS
from weekly_dates import week_id_to_period, prev_week_id
from weekly_db import WeeklyDB, WeeklyDBError, ProductionGuardError
from weekly_secrets import load_secrets, SecretsError, mask_secret
from weekly_snapshot import (
    build_snapshot_record,
    check_snapshots,
    print_preview,
)
from weekly_sources import fetch_week_observations, select_last_valid


def _confirm(prompt: str) -> bool:
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans == "yes"


def main() -> None:
    parser = argparse.ArgumentParser(description="週次スナップショット取得・保存")
    parser.add_argument("--week-id",    required=True, help="例: 2026-W26")
    parser.add_argument("--dry-run",    action="store_true", help="DB 書き込みを行わない")
    parser.add_argument("--production", action="store_true", help="本番 Supabase への書き込みを許可")
    args = parser.parse_args()

    week_id = args.week_id
    dry_run = args.dry_run

    # 1. week_id 検証
    try:
        period_start, period_end = week_id_to_period(week_id)
    except ValueError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"対象週: {week_id}  ({period_start} 〜 {period_end})")

    # 2. 秘密情報読込
    try:
        secrets = load_secrets()
    except SecretsError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    # 3. DB クライアント初期化
    db = WeeklyDB(secrets["SUPABASE_URL"], secrets["SUPABASE_SERVICE_ROLE_KEY"])
    if db.is_production():
        if not args.production:
            print(
                "[HARD] 本番 Supabase が指定されています。\n"
                "  本番へ書き込む場合は --production フラグを指定してください。\n"
                "  dry-run で確認するには --dry-run を使用してください。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("⚠  [警告] 本番 Supabase への書き込みモードです。")

    # 4. 6 資産の日次系列取得 & 観測値選択
    fred_key = secrets["FRED_API_KEY"]
    fetch_results: dict = {}
    fetch_errors: list[str] = []

    for cfg in ASSET_CONFIGS:
        key = cfg["asset_key"]
        try:
            obs_list, src = fetch_week_observations(cfg, fred_key, period_start, period_end)
            obs = select_last_valid(obs_list)
            fetch_results[key] = (obs, src)
            if obs is None:
                print(f"  {key}: no_data (週内に有効値なし)")
        except Exception as e:
            err_msg = mask_secret(str(e), secrets)[:120]
            print(f"  {key}: 取得エラー — {err_msg}")
            fetch_results[key] = (None, f"error_{key}")
            fetch_errors.append(f"{key}: {err_msg}")

    # 5. スナップレコード構築
    snapshot_taken_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    error_keys = {k for k, (_, src) in fetch_results.items() if src.startswith("error_")}
    records = []
    for cfg in ASSET_CONFIGS:
        key = cfg["asset_key"]
        obs, src = fetch_results.get(key, (None, f"error_{key}"))
        rec = build_snapshot_record(cfg, obs, src, snapshot_taken_at, week_id)
        if key in error_keys:
            rec["status"] = "error"
        records.append(rec)

    # 6. HARD/WARN 判定
    hard, warn = check_snapshots(records, period_start, period_end)

    # 7. プレビュー表示
    print_preview(records, hard, warn, week_id)

    if hard:
        print("[HARD] 停止条件を検出しました。DB への書き込みを中止します。", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print("[DRY-RUN] DB 書き込みをスキップしました。")
        sys.exit(0)

    # 8. 確認プロンプト
    if not _confirm(f"{week_id} の 6 資産スナップを保存しますか？ [yes/no]: "):
        print("キャンセルしました。")
        sys.exit(0)

    # 9. Supabase upsert
    try:
        saved = db.upsert_snapshots(records, allow_production=args.production)
        print(f"✓ {saved} 件を保存しました ({week_id})")
    except ProductionGuardError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)
    except WeeklyDBError as e:
        err_msg = mask_secret(str(e), secrets)
        print(f"[HARD] DB 書き込み失敗: {err_msg}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
