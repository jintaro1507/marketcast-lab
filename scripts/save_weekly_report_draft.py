#!/usr/bin/env python3
"""
save_weekly_report_draft.py — Weekly Marketcast draft を weekly_reports へ保存

使用例:
  python scripts/save_weekly_report_draft.py --week-id 2026-W26
  python scripts/save_weekly_report_draft.py --week-id 2026-W26 --dry-run
  python scripts/save_weekly_report_draft.py --week-id 2026-W26 --draft-path /path/to/draft.json

--dry-run:      draft 読込・検証・プレビューまで実施し、DB 書き込みを行わない。
--production:   本番 Supabase への書き込みを許可（省略時はローカルのみ）。
--draft-path:   draft ファイルのパスを明示指定（省略時はデフォルト保存先）。

制約:
  - published / withdrawn 行を上書きしない
  - 同一 week_id の異なる draft は拒否（冪等成功のみ許可）
  - teaser_hash / paid_body_hash は DB に保存しない（W2-4 で付与）
  - 本番 Supabase には --production なしで書き込まない
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from weekly_dates import week_id_to_period
from weekly_db import WeeklyDBError, ProductionGuardError
from weekly_report_db import (
    WeeklyReportDB,
    DraftValidationError,
    validate_draft,
    build_db_payload,
    are_drafts_equal,
    verify_saved_report,
)
from weekly_secrets import load_secrets, SecretsError

# デフォルト draft 保存先
_DRAFTS_DIR = Path.home() / ".local" / "share" / "marketcast-lab" / "drafts"


# ─── draft 読み込み ───────────────────────────────────────────────────────────

def load_draft(week_id: str, draft_path: Path | None) -> dict:
    if draft_path is not None:
        path = draft_path
    else:
        path = _DRAFTS_DIR / f"{week_id}_draft.json"

    if not path.exists():
        raise FileNotFoundError(
            f"draft ファイルが見つかりません: {path}\n"
            f"  generate_weekly_draft.py で先に draft を生成してください。"
        )

    with open(path, encoding="utf-8") as f:
        draft = json.load(f)

    return draft


# ─── プレビュー表示 ───────────────────────────────────────────────────────────

def print_preview(draft: dict, payload: dict, warnings: list[str]) -> None:
    ft  = payload["free_teaser"]
    sep = "=" * 60
    print(sep)
    print("  WEEKLY MARKETCAST DRAFT — DB 保存プレビュー")
    print(sep)
    print(f"  week_id      : {payload['week_id']}")
    print(f"  title        : {payload['title']}")
    print(f"  period_start : {payload['period_start']}")
    print(f"  period_end   : {payload['period_end']}")
    print(f"  revision     : {payload['revision']}")
    print(f"  generated_at : {payload['generated_at']}")
    print(f"  status       : draft（固定）")
    print(f"  teaser_hash  : NULL（W2-4 で付与）")
    print(f"  paid_body_hash: NULL（W2-4 で付与）")
    print(f"  free_teaser Schema: OK")
    print(f"  paid_body Schema : OK")
    print(f"  hash 再計算     : OK")
    print(f"  restricted 漏洩 : なし")
    if warnings:
        print(f"\n  WARN ({len(warnings)}件):")
        for w in warnings:
            # 長い場合は切り詰め
            print(f"    {w[:120]}")
    else:
        print(f"\n  WARN: なし")
    print(sep)


# ─── 確認プロンプト ───────────────────────────────────────────────────────────

def _confirm(prompt: str) -> bool:
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans == "yes"


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weekly Marketcast draft を weekly_reports へ保存"
    )
    parser.add_argument("--week-id",    required=True, help="例: 2026-W26")
    parser.add_argument("--draft-path", type=Path,     help="draft ファイルのパスを明示指定")
    parser.add_argument("--dry-run",    action="store_true", help="DB 書き込みを行わない")
    parser.add_argument("--production", action="store_true", help="本番 Supabase への書き込みを許可")
    args = parser.parse_args()

    week_id  = args.week_id
    dry_run  = args.dry_run

    print(f"[W2-3] Weekly Marketcast draft 保存 — {week_id}")
    if dry_run:
        print("  [dry-run モード] DB への書き込みは行いません")

    # 1. week_id 検証
    try:
        period_start, period_end = week_id_to_period(week_id)
    except ValueError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  対象週: {period_start} 〜 {period_end}")

    # 2. draft 読み込み
    print("  draft 読み込み中...")
    try:
        draft = load_draft(week_id, args.draft_path)
    except FileNotFoundError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[HARD] draft ファイル読み込みエラー: {e}", file=sys.stderr)
        sys.exit(1)

    warnings: list[str] = draft.get("warnings", [])
    if warnings:
        print(f"  WARN ({len(warnings)}件):")
        for w in warnings:
            print(f"    {w[:120]}")

    # 3. 保存前検証
    print("  保存前検証中...")
    errors = validate_draft(draft, week_id)
    if errors:
        print("[HARD] draft 検証エラー:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    print("  検証 OK（Schema・hash・restricted・period）")

    # 4. DB payload 構築
    payload = build_db_payload(draft)

    # 5. プレビュー表示
    print_preview(draft, payload, warnings)

    # 6. dry-run はここで終了
    if dry_run:
        print("\ndry-run: DBへの書き込みは行っていません。")
        sys.exit(0)

    # 7. 秘密情報読み込み
    try:
        secrets = load_secrets()
    except SecretsError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    # 8. DB クライアント初期化
    db = WeeklyReportDB(secrets["SUPABASE_URL"], secrets["SUPABASE_SERVICE_ROLE_KEY"])

    if db.is_production():
        if not args.production:
            print(
                "[HARD] 本番 Supabase が指定されています。\n"
                "  本番へ書き込む場合は --production フラグを指定してください。\n"
                "  dry-run で確認するには --dry-run を使用してください。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("  [本番モード] 本番 Supabase へ書き込みます")

    # 9. 既存行を確認
    print("  既存行確認中...")
    try:
        existing = db.get_report_full(week_id)
    except WeeklyDBError as e:
        print(f"[HARD] DB 参照エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if existing is not None:
        status = existing.get("status", "")

        if status == "published":
            print(
                f"[HARD] 既に published です。draft で上書きできません。\n"
                f"  week_id={week_id}",
                file=sys.stderr,
            )
            sys.exit(1)

        if status == "withdrawn":
            print(
                f"[HARD] 既に withdrawn です。\n"
                f"  withdrawn 後の再発行は revision 設計を含む公開後機能として扱います。\n"
                f"  week_id={week_id}",
                file=sys.stderr,
            )
            sys.exit(1)

        if status == "draft":
            if are_drafts_equal(existing, payload):
                print(f"既に同一 draft が保存されています。（冪等終了）")
                sys.exit(0)
            else:
                print(
                    f"[HARD] 同じ week_id に異なる draft が存在します。\n"
                    f"  week_id={week_id}\n"
                    f"  既存 draft を上書きする場合は、DB の既存行を先に確認してください。",
                    file=sys.stderr,
                )
                sys.exit(1)

    # 10. 確認プロンプト
    if not _confirm(
        f"\n{week_id} の Weekly Marketcast draft を DB へ保存しますか？ [yes/no] "
    ):
        print("キャンセルしました。")
        sys.exit(0)

    # 11. INSERT
    print("  INSERT 中...")
    try:
        saved_row = db.insert_report_draft(payload, allow_production=args.production)
    except ProductionGuardError as e:
        print(f"[HARD] 本番ガード: {e}", file=sys.stderr)
        sys.exit(1)
    except WeeklyDBError as e:
        print(f"[HARD] INSERT エラー: {e}", file=sys.stderr)
        sys.exit(1)

    # 12. 再取得して整合確認
    print("  保存後確認中...")
    try:
        db_row = db.get_report_full(week_id)
    except WeeklyDBError as e:
        print(
            f"[警告] INSERT は成功しましたが、再取得に失敗しました（状態不明）: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    if db_row is None:
        print(
            "[HARD] INSERT 後に行が見つかりませんでした（状態不明）",
            file=sys.stderr,
        )
        sys.exit(1)

    mismatches = verify_saved_report(db_row, payload)
    if mismatches:
        print("[HARD] 保存後整合確認に失敗しました:", file=sys.stderr)
        for m in mismatches:
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)

    # 13. 成功表示
    print(f"\n[完了] {week_id} の draft を weekly_reports へ保存しました。")
    print(f"  status=draft / teaser_hash=NULL / paid_body_hash=NULL")
    print(f"  次のステップ: draft レビュー → 承認 → 公開（W2-4 以降）")


if __name__ == "__main__":
    main()
