#!/usr/bin/env python3
"""
approve_weekly_report.py — Weekly Marketcast 承認 CLI

使用例:
  OPERATOR_NAME=your-name python scripts/approve_weekly_report.py --week-id 2026-W26
  OPERATOR_NAME=your-name python scripts/approve_weekly_report.py --week-id 2026-W26 --dry-run
  OPERATOR_NAME=your-name python scripts/approve_weekly_report.py \\
    --week-id 2026-W26 --draft-path /path/to/draft.json

環境変数:
  OPERATOR_NAME  承認者識別子（必須, 1〜64 文字, CLI フラグ不可）

制約:
  - DB は変更しない（status=draft のまま）
  - Pages JSON は生成しない
  - git push は行わない
  - 本番 Supabase には --production なしで接続しない
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from weekly_dates import week_id_to_period
from weekly_db import WeeklyDBError, ProductionGuardError
from weekly_report_db import WeeklyReportDB, validate_draft
from weekly_secrets import load_secrets, SecretsError
from weekly_approval import (
    get_operator_name,
    build_approval_payload,
    validate_approval_schema,
    are_approvals_equal,
    load_approval,
    save_approval,
    verify_db_draft_matches_local,
    parse_approval_input,
    OperatorNameError,
    ApprovalFileError,
)

_DRAFTS_DIR = Path.home() / ".local" / "share" / "marketcast-lab" / "drafts"


def _load_draft(week_id: str, draft_path: Path | None) -> dict:
    p = draft_path if draft_path is not None else _DRAFTS_DIR / f"{week_id}_draft.json"
    if not p.exists():
        raise FileNotFoundError(
            f"draft ファイルが見つかりません: {p}\n"
            f"  generate_weekly_draft.py で先に draft を生成してください。"
        )
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _print_preview(draft: dict, operator_name: str) -> None:
    ft  = draft["free_teaser"]
    pb  = draft["paid_body"]
    sep = "=" * 60
    print(sep)
    print("  WEEKLY MARKETCAST — 承認プレビュー")
    print(sep)
    print(f"  week_id        : {draft['week_id']}")
    print(f"  revision       : {draft['revision']}")
    print(f"  generated_at   : {draft['generated_at']}")
    print(f"  env_label      : {ft.get('env_label', '—')}")
    print(f"  title          : {ft.get('title', '—')}")
    print(f"  teaser_summary : {str(ft.get('teaser_summary', '—'))[:80]}")
    theme = ft.get("featured_theme", {})
    print(f"  featured_theme : {theme.get('label', '—')}")
    similar = pb.get("similar_events", [])
    if similar:
        print(f"  類似局面       :")
        for ev in similar[:3]:
            print(f"    [{ev.get('rank')}] {ev.get('event_name')} ({ev.get('event_date')})")
    obs = pb.get("observation_points", [])
    if obs:
        print(f"  観測点         :")
        for o in obs[:3]:
            print(f"    · {str(o)[:80]}")
    print(f"  teaser_hash    : {draft['teaser_hash'][:8]}...")
    print(f"  paid_body_hash : {draft['paid_body_hash'][:8]}...")
    print(f"  承認者         : {operator_name}")
    print(sep)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weekly Marketcast draft を承認する（DB は変更しない）"
    )
    parser.add_argument("--week-id",    required=True, help="例: 2026-W26")
    parser.add_argument("--draft-path", type=Path,     help="draft ファイルのパスを明示指定")
    parser.add_argument("--dry-run",    action="store_true", help="承認ファイルへの書き込みを行わない")
    parser.add_argument("--production", action="store_true", help="本番 Supabase への接続を許可")
    args = parser.parse_args()

    week_id = args.week_id
    dry_run = args.dry_run

    print(f"[W2-4A] Weekly Marketcast 承認 — {week_id}")
    if dry_run:
        print("  [dry-run モード] 承認ファイルへの書き込みは行いません")

    # 1. week_id 検証
    try:
        period_start, period_end = week_id_to_period(week_id)
    except ValueError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  対象週: {period_start} 〜 {period_end}")

    # 2. OPERATOR_NAME（環境変数のみ, CLI フラグ不可）
    try:
        operator_name = get_operator_name()
    except OperatorNameError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    # 3. draft 読み込み
    print("  draft 読み込み中...")
    try:
        draft = _load_draft(week_id, args.draft_path)
    except FileNotFoundError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[HARD] draft ファイル読み込みエラー: {e}", file=sys.stderr)
        sys.exit(1)

    # 4. draft 検証
    print("  draft 検証中...")
    errors = validate_draft(draft, week_id)
    if errors:
        print("[HARD] draft 検証エラー:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    print("  検証 OK")

    warnings = draft.get("warnings", [])
    if warnings:
        print(f"\n  WARN ({len(warnings)} 件):")
        for w in warnings:
            print(f"    {str(w)[:120]}")

    # 5. プレビュー表示
    _print_preview(draft, operator_name)

    # 6. dry-run はここで終了
    if dry_run:
        print("\ndry-run: 承認ファイルへの書き込みは行っていません。")
        sys.exit(0)

    # 7. 秘密情報読み込み
    try:
        secrets = load_secrets()
    except SecretsError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    # 8. DB クライアント初期化・本番ガード
    db = WeeklyReportDB(secrets["SUPABASE_URL"], secrets["SUPABASE_SERVICE_ROLE_KEY"])
    if db.is_production():
        if not args.production:
            print(
                "[HARD] 本番 Supabase が指定されています。\n"
                "  --production フラグを指定してください。",
                file=sys.stderr,
            )
            sys.exit(1)
        print("  [本番モード] 本番 Supabase に接続します（DB は変更しません）")

    # 9. DB から draft 行を取得し、ローカルと照合
    print("  DB 確認中...")
    try:
        db_row = db.get_report_full(week_id)
    except WeeklyDBError as e:
        print(f"[HARD] DB 参照エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if db_row is None:
        print(
            f"[HARD] DB に {week_id} の draft 行が存在しません。\n"
            f"  save_weekly_report_draft.py で先に DB へ保存してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    db_errors = verify_db_draft_matches_local(db_row, draft)
    if db_errors:
        print("[HARD] DB draft とローカル draft が一致しません:", file=sys.stderr)
        for e in db_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    print("  DB 確認 OK")

    # 10. 既存承認ファイルの確認（冪等性）
    now_utc = datetime.now(timezone.utc).isoformat()
    new_approval = build_approval_payload(week_id, draft, operator_name, now_utc)

    try:
        existing = load_approval(week_id)
    except ApprovalFileError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    if existing is not None:
        if are_approvals_equal(existing, new_approval):
            print(f"\n[完了] 既に同一の承認ファイルが存在します。（冪等終了）")
            print(f"  {week_id}_approval.json は変更されていません。")
            sys.exit(0)
        else:
            print(
                f"[HARD] 同じ week_id に異なる承認ファイルが存在します。\n"
                f"  week_id={week_id}\n"
                f"  異なる承認を記録する場合は既存ファイルを先に確認してください。",
                file=sys.stderr,
            )
            sys.exit(1)

    # 11. 承認 payload スキーマ検証
    schema_errors = validate_approval_schema(new_approval)
    if schema_errors:
        print("[HARD] 承認 payload スキーマエラー:", file=sys.stderr)
        for e in schema_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    # 12. 明示的承認入力（"APPROVE YYYY-WXX" の完全一致）
    print(f"\n承認を記録するには「APPROVE {week_id}」と入力してください。")
    print("（キャンセルするには Enter を押してください）")
    try:
        user_input = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nキャンセルしました。")
        sys.exit(0)

    if not parse_approval_input(user_input, week_id):
        print(f"入力が一致しませんでした（入力: {user_input!r}）。キャンセルしました。")
        sys.exit(0)

    # 13. 承認ファイル保存（ディレクトリ 700 / ファイル 600）
    print("  承認ファイル保存中...")
    try:
        saved_path = save_approval(new_approval)
    except ApprovalFileError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    # 14. 成功表示
    print(f"\n[完了] {week_id} の承認を記録しました。")
    print(f"  承認ファイル   : {saved_path}")
    print(f"  承認者         : {operator_name}")
    print(f"  teaser_hash    : {new_approval['teaser_hash'][:8]}...")
    print(f"  paid_body_hash : {new_approval['paid_body_hash'][:8]}...")
    print(f"  DB の status   : draft のまま（変更なし）")
    print(f"  次のステップ   : Pages 反映確認 → published 遷移（W2-4B 以降）")


if __name__ == "__main__":
    main()
