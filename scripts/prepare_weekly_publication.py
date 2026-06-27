#!/usr/bin/env python3
"""
prepare_weekly_publication.py — 承認済み draft から公開ファイルを生成

使用例:
  python scripts/prepare_weekly_publication.py --week-id 2026-W26
  python scripts/prepare_weekly_publication.py --week-id 2026-W26 --dry-run
  python scripts/prepare_weekly_publication.py --week-id 2026-W26 \\
    --draft-path /path/to/draft.json \\
    --approval-path /path/to/approval.json

実行後:
  - data/weekly/YYYY-WXX.json（公開 teaser）
  - data/weekly/index.json（インデックス）
  - ~/.local/share/marketcast-lab/publications/YYYY-WXX_publication.json（pub state）

  operator が git add / git commit / git push を手動実施する。

制約:
  - git commit/push は行わない
  - paid_body / restricted 生値を公開 JSON に含めない
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
    load_approval,
    validate_approval_schema,
    verify_db_draft_matches_local,
    ApprovalFileError,
)
from weekly_report_publish import verify_pre_publish
from weekly_pages import (
    build_initial_pub_state,
    build_public_teaser,
    build_index_entry,
    check_forbidden_keys_deep,
    load_pub_state,
    load_public_index,
    save_pub_state,
    update_index,
    validate_public_index,
    validate_public_teaser,
    write_public_index,
    write_public_teaser,
    IndexUpdateError,
    PUBLIC_TEASER_DIR,
    INDEX_PATH,
    PubStateError,
    PublicFileError,
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


def _print_preview(teaser: dict) -> None:
    ft  = teaser.get("free_teaser", {})
    sep = "=" * 60
    print(sep)
    print("  WEEKLY MARKETCAST — 公開プレビュー")
    print(sep)
    print(f"  week_id        : {teaser['week_id']}")
    print(f"  revision       : {teaser['revision']}")
    print(f"  published_at   : {teaser['published_at']}")
    print(f"  env_label      : {ft.get('env_label', '—')}")
    print(f"  title          : {ft.get('title', '—')}")
    print(f"  teaser_summary : {str(ft.get('teaser_summary', '—'))[:80]}")
    theme = ft.get("featured_theme") or {}
    print(f"  featured_theme : {theme.get('label', '（なし）')}")
    top = ft.get("top_match_preview") or {}
    if top:
        print(f"  top_match      : {top.get('event_name')} ({top.get('event_date')})")
    print(f"  teaser_hash    : {teaser.get('teaser_hash', '')[:8]}...")
    print(f"  teaser_file    : data/weekly/{teaser['week_id']}.json")
    print(f"  index_file     : data/weekly/index.json")
    print(sep)


def _parse_prepare_input(user_input: str, week_id: str) -> bool:
    return user_input.strip() == f"PREPARE {week_id}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="承認済み Weekly Marketcast draft から公開ファイルを生成する"
    )
    parser.add_argument("--week-id",       required=True, help="例: 2026-W26")
    parser.add_argument("--draft-path",    type=Path,     help="draft ファイルのパスを明示指定")
    parser.add_argument("--approval-path", type=Path,     help="承認ファイルのパスを明示指定")
    parser.add_argument("--dry-run",       action="store_true", help="ファイルへの書き込みを行わない")
    parser.add_argument("--production",    action="store_true", help="本番 Supabase への接続を許可")
    args = parser.parse_args()

    week_id = args.week_id
    dry_run = args.dry_run

    print(f"[W2-4B] Weekly Marketcast 公開準備 — {week_id}")
    if dry_run:
        print("  [dry-run モード] ファイルへの書き込みは行いません")

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
        draft = _load_draft(week_id, args.draft_path)
    except FileNotFoundError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[HARD] draft ファイル読み込みエラー: {e}", file=sys.stderr)
        sys.exit(1)

    errors = validate_draft(draft, week_id)
    if errors:
        print("[HARD] draft 検証エラー:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    # 3. 承認ファイル読み込み
    print("  承認ファイル読み込み中...")
    try:
        approval = load_approval(week_id, path=args.approval_path)
    except ApprovalFileError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    if approval is None:
        print(
            f"[HARD] {week_id} の承認ファイルが見つかりません。\n"
            f"  approve_weekly_report.py で先に承認を記録してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    schema_errors = validate_approval_schema(approval)
    if schema_errors:
        print("[HARD] 承認ファイルスキーマエラー:", file=sys.stderr)
        for e in schema_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    # 4. 既存 pub_state 確認（冪等性・ステージ整合）
    try:
        pub_state = load_pub_state(week_id)
    except PubStateError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    if pub_state is not None:
        stage = pub_state.get("stage")
        if stage != "prepared":
            print(
                f"[HARD] 公開ステートのステージが 'prepared' ではありません: stage={stage!r}\n"
                f"  既にワークフローが進行しています。",
                file=sys.stderr,
            )
            sys.exit(1)
        # 同一内容か確認
        if (pub_state.get("teaser_hash") == approval.get("teaser_hash") and
                pub_state.get("paid_body_hash") == approval.get("paid_body_hash") and
                pub_state.get("revision") == approval.get("revision")):
            print(f"\n[完了] 既に同一内容の公開ステートが存在します。（冪等終了）")
            print(f"  次のステップ: git add data/weekly/ → git commit → git push")
            print(f"  その後: verify_weekly_pages.py --week-id {week_id}")
            sys.exit(0)
        else:
            print(
                f"[HARD] 既存の公開ステートと承認ファイルの内容が一致しません。\n"
                f"  pub_state teaser_hash: {pub_state.get('teaser_hash', '?')[:16]}...\n"
                f"  approval  teaser_hash: {approval.get('teaser_hash', '?')[:16]}...",
                file=sys.stderr,
            )
            sys.exit(1)

    # 5. 秘密情報読み込み・DB 接続
    try:
        secrets = load_secrets()
    except SecretsError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    db = WeeklyReportDB(secrets["SUPABASE_URL"], secrets["SUPABASE_SERVICE_ROLE_KEY"])
    if db.is_production() and not args.production:
        print(
            "[HARD] 本番 Supabase が指定されています。\n"
            "  --production フラグを指定してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    # 6. DB 確認
    print("  DB 確認中...")
    try:
        db_row = db.get_report_full(week_id)
    except WeeklyDBError as e:
        print(f"[HARD] DB 参照エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if db_row is None:
        print(
            f"[HARD] DB に {week_id} の行が存在しません。",
            file=sys.stderr,
        )
        sys.exit(1)

    db_errors = verify_db_draft_matches_local(db_row, draft)
    if db_errors:
        print("[HARD] DB draft とローカル draft が一致しません:", file=sys.stderr)
        for e in db_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    # 7. 公開前改変チェック
    print("  公開前改変チェック中...")
    pre_errors = verify_pre_publish(approval, draft, db_row)
    if pre_errors:
        print("[HARD] 公開前検証エラー:", file=sys.stderr)
        for e in pre_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    print("  検証 OK")

    # 8. 公開 teaser 構築
    published_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    new_pub_state = build_initial_pub_state(week_id, approval, published_at)
    teaser = build_public_teaser(draft, approval, new_pub_state)

    teaser_errors = validate_public_teaser(teaser)
    if teaser_errors:
        print("[HARD] 公開 teaser 検証エラー:", file=sys.stderr)
        for e in teaser_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    # 9. インデックス更新準備
    index_entry = build_index_entry(teaser)
    existing_index = load_public_index(INDEX_PATH)
    try:
        new_index, index_action = update_index(existing_index, index_entry)
    except IndexUpdateError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    index_errors = validate_public_index(new_index)
    if index_errors:
        print("[HARD] インデックス検証エラー:", file=sys.stderr)
        for e in index_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    # 10. プレビュー表示
    _print_preview(teaser)

    # 11. dry-run はここで終了
    if dry_run:
        print("\ndry-run: ファイルへの書き込みは行っていません。")
        sys.exit(0)

    # 12. 明示的確認入力（"PREPARE YYYY-WXX" の完全一致）
    teaser_path = PUBLIC_TEASER_DIR / f"{week_id}.json"
    print(f"\n以下のファイルを書き込みます:")
    print(f"  {teaser_path}")
    print(f"  {INDEX_PATH}")
    print(f"  index: {index_action}")
    print(f"\n公開ファイルを生成するには「PREPARE {week_id}」と入力してください。")
    print("（キャンセルするには Enter を押してください）")
    try:
        user_input = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nキャンセルしました。")
        sys.exit(0)

    if not _parse_prepare_input(user_input, week_id):
        print(f"入力が一致しませんでした（入力: {user_input!r}）。キャンセルしました。")
        sys.exit(0)

    # 13. teaser ファイル書き込み
    print("  teaser ファイル書き込み中...")
    try:
        teaser_result = write_public_teaser(teaser, teaser_path)
    except PublicFileError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  teaser: {teaser_result}")

    # 14. インデックス書き込み
    print("  index 書き込み中...")
    write_public_index(new_index, INDEX_PATH)
    print(f"  index: {index_action}")

    # 15. pub state 保存
    print("  pub state 保存中...")
    saved_path = save_pub_state(new_pub_state)

    # 16. 完了表示
    print(f"\n[完了] {week_id} の公開ファイルを生成しました。")
    print(f"  teaser_file : {teaser_path}")
    print(f"  index_file  : {INDEX_PATH}")
    print(f"  pub_state   : {saved_path}")
    print(f"  published_at: {published_at}")
    print(f"\n次のステップ:")
    print(f"  1. git add data/weekly/{week_id}.json data/weekly/index.json")
    print(f"  2. git commit -m 'Publish Weekly Marketcast {week_id}'")
    print(f"  3. git push")
    print(f"  4. python scripts/verify_weekly_pages.py --week-id {week_id}")


if __name__ == "__main__":
    main()
