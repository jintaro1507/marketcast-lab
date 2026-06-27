#!/usr/bin/env python3
"""
seed_initial_snapshot.py — 初号スナップショット seed 投入

使用例:
  python scripts/seed_initial_snapshot.py --week-id 2026-W25
  python scripts/seed_initial_snapshot.py --week-id 2026-W25 --dry-run
  python scripts/seed_initial_snapshot.py --week-id 2026-W25 --production

指定 week-id のスナップを seeded=True で保存する。
  - 同一週に通常スナップが既に存在する場合は停止する。
  - 同一週に seed 済みの場合も停止する（上書きしない）。
  - --replace は初期版では実装しない。

seed_source は固定文字列 "initial_seed_from_source_series" を使用する。
"""

import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from weekly_config import ASSET_CONFIGS
from weekly_dates import week_id_to_period
from weekly_db import WeeklyDB, WeeklyDBError, ProductionGuardError
from weekly_secrets import load_secrets, SecretsError, mask_secret
from weekly_snapshot import (
    build_snapshot_record,
    check_snapshots,
    print_preview,
)
from weekly_sources import fetch_week_observations, select_last_valid

SEED_SOURCE = "initial_seed_from_source_series"


def _confirm(prompt: str) -> bool:
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans == "yes"


def main() -> None:
    parser = argparse.ArgumentParser(description="初号スナップショット seed 投入")
    parser.add_argument("--week-id",    required=True, help="seed する週 (例: 2026-W25)")
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

    print(f"seed 対象週: {week_id}  ({period_start} 〜 {period_end})")
    print(f"seed_source: {SEED_SOURCE}")

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
                "  本番へ seed する場合は --production フラグを指定してください。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("⚠  [警告] 本番 Supabase への seed モードです。")

    # 4. 既存スナップ確認（dry-run 以外）
    if not dry_run:
        existing = db.get_snapshots(week_id)
        if existing:
            has_normal = any(not r.get("seeded") for r in existing)
            has_seed   = any(r.get("seeded") for r in existing)
            if has_normal:
                print(
                    f"[HARD] {week_id} に通常スナップ（seeded=false）が既に存在します。\n"
                    "  seed は通常スナップが存在しない週のみ投入できます。",
                    file=sys.stderr,
                )
                sys.exit(1)
            if has_seed:
                print(
                    f"[HARD] {week_id} には seed スナップが既に存在します。\n"
                    "  再投入するには既存レコードを手動で削除してください（--replace は未実装）。",
                    file=sys.stderr,
                )
                sys.exit(1)

    # 5. 6 資産の日次系列取得 & 観測値選択
    fred_key = secrets["FRED_API_KEY"]
    fetch_results: dict = {}

    for cfg in ASSET_CONFIGS:
        key = cfg["asset_key"]
        try:
            obs_list, src = fetch_week_observations(cfg, fred_key, period_start, period_end)
            obs = select_last_valid(obs_list)
            fetch_results[key] = (obs, src)
        except Exception as e:
            err_msg = mask_secret(str(e), secrets)[:120]
            print(f"  {key}: 取得エラー — {err_msg}")
            fetch_results[key] = (None, f"error_{key}")

    # 6. スナップレコード構築（seeded=True）
    snapshot_taken_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    records = []
    for cfg in ASSET_CONFIGS:
        key = cfg["asset_key"]
        obs, src = fetch_results.get(key, (None, "error"))
        rec = build_snapshot_record(
            cfg, obs, src, snapshot_taken_at, week_id,
            seeded=True, seed_source=SEED_SOURCE,
        )
        records.append(rec)

    # 7. HARD/WARN 判定
    hard, warn = check_snapshots(records, period_start, period_end)

    # 8. プレビュー表示
    print_preview(records, hard, warn, week_id)

    if hard:
        print("[HARD] 停止条件を検出しました。seed を中止します。", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print("[DRY-RUN] DB 書き込みをスキップしました。")
        sys.exit(0)

    # 9. 確認プロンプト
    if not _confirm(f"{week_id} に seed スナップを保存しますか？ [yes/no]: "):
        print("キャンセルしました。")
        sys.exit(0)

    # 10. Supabase upsert
    try:
        saved = db.upsert_snapshots(records, allow_production=args.production)
        print(f"✓ seed {saved} 件を保存しました ({week_id})")
    except ProductionGuardError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)
    except WeeklyDBError as e:
        err_msg = mask_secret(str(e), secrets)
        print(f"[HARD] DB 書き込み失敗: {err_msg}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
