#!/usr/bin/env python3
"""
finalize_weekly_publication.py — Pages 確認後の DB published 化と draft アーカイブ

pages_verified 状態の公開ステートに対して実行する。
DB を draft → published に遷移させ、draft をアーカイブして完了とする。

使用例:
  OPERATOR_NAME=your-name python scripts/finalize_weekly_publication.py --week-id 2026-W26
  OPERATOR_NAME=your-name python scripts/finalize_weekly_publication.py --week-id 2026-W26 --dry-run

環境変数:
  OPERATOR_NAME  承認者識別子（必須、approve_weekly_report.py で設定した値と一致する必要がある）

制約:
  - 本番 Supabase には --production なしで接続しない
  - git commit/push は行わない
  - 自動リトライなし（Pages 再検証は 1 パスのみ）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from weekly_approval import (
    get_operator_name,
    load_approval,
    validate_approval_schema,
    verify_db_draft_matches_local,
    OperatorNameError,
    ApprovalFileError,
)
from weekly_dates import week_id_to_period
from weekly_db import WeeklyDBError, ProductionGuardError
from weekly_report_builder import compute_hash
from weekly_report_db import WeeklyReportDB, validate_draft
from weekly_report_publish import (
    build_publish_payload,
    verify_pre_publish,
    verify_published_report,
)
from weekly_secrets import load_secrets, SecretsError
from weekly_pages import (
    advance_stage,
    archive_draft,
    build_index_entry,
    load_pub_state,
    save_pub_state,
    verify_deployed_index,
    verify_deployed_teaser,
    PUBLIC_TEASER_DIR,
    PubStateError,
)

_DRAFTS_DIR = Path.home() / ".local" / "share" / "marketcast-lab" / "drafts"


def _load_draft(week_id: str, draft_path: Path | None) -> dict:
    p = draft_path if draft_path is not None else _DRAFTS_DIR / f"{week_id}_draft.json"
    if not p.exists():
        raise FileNotFoundError(f"draft ファイルが見つかりません: {p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _parse_publish_input(user_input: str, week_id: str) -> bool:
    return user_input.strip() == f"PUBLISH {week_id}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pages 確認後に DB を published 化し draft をアーカイブする"
    )
    parser.add_argument("--week-id",       required=True, help="例: 2026-W26")
    parser.add_argument("--draft-path",    type=Path,     help="draft ファイルのパスを明示指定")
    parser.add_argument("--approval-path", type=Path,     help="承認ファイルのパスを明示指定")
    parser.add_argument("--dry-run",       action="store_true", help="DB 更新・アーカイブを行わない")
    parser.add_argument("--production",    action="store_true", help="本番 Supabase への接続を許可")
    args = parser.parse_args()

    week_id = args.week_id
    dry_run = args.dry_run

    print(f"[W2-4B] Weekly Marketcast 公開完了処理 — {week_id}")
    if dry_run:
        print("  [dry-run モード] DB 更新・アーカイブは行いません")

    # 1. pub state 読み込み・ステージ確認
    try:
        pub_state = load_pub_state(week_id)
    except PubStateError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    if pub_state is None:
        print(
            f"[HARD] {week_id} の公開ステートが見つかりません。\n"
            f"  prepare_weekly_publication.py を先に実行してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    stage = pub_state.get("stage")
    if stage == "completed":
        print(f"[完了] 既に completed 状態です。（冪等終了）")
        sys.exit(0)
    if stage != "pages_verified":
        print(
            f"[HARD] pub state のステージが 'pages_verified' ではありません: stage={stage!r}\n"
            f"  verify_weekly_pages.py を先に実行してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    # 2. OPERATOR_NAME 確認
    try:
        operator_name = get_operator_name()
    except OperatorNameError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    # 3. draft・承認ファイル読み込み
    print("  draft / 承認ファイル読み込み中...")
    try:
        draft = _load_draft(week_id, args.draft_path)
    except FileNotFoundError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[HARD] draft 読み込みエラー: {e}", file=sys.stderr)
        sys.exit(1)

    errors = validate_draft(draft, week_id)
    if errors:
        print("[HARD] draft 検証エラー:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    try:
        approval = load_approval(week_id, path=args.approval_path)
    except ApprovalFileError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    if approval is None:
        print(f"[HARD] {week_id} の承認ファイルが見つかりません。", file=sys.stderr)
        sys.exit(1)

    schema_errors = validate_approval_schema(approval)
    if schema_errors:
        print("[HARD] 承認ファイルスキーマエラー:", file=sys.stderr)
        for e in schema_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    # 4. OPERATOR_NAME == approved_by 確認
    if operator_name != approval.get("approved_by"):
        print(
            f"[HARD] OPERATOR_NAME と承認ファイルの approved_by が一致しません。\n"
            f"  OPERATOR_NAME : {operator_name!r}\n"
            f"  approved_by   : {approval.get('approved_by')!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    # 5. pub state との整合確認
    if pub_state.get("teaser_hash") != approval.get("teaser_hash"):
        print(
            f"[HARD] pub state の teaser_hash が承認ファイルと不一致です。\n"
            f"  pub_state : {pub_state.get('teaser_hash', '?')[:16]}...\n"
            f"  approval  : {approval.get('teaser_hash', '?')[:16]}...",
            file=sys.stderr,
        )
        sys.exit(1)

    # 6. 秘密情報読み込み・DB 接続
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

    # 7. DB 再確認
    print("  DB 確認中...")
    try:
        db_row = db.get_report_full(week_id)
    except WeeklyDBError as e:
        print(f"[HARD] DB 参照エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if db_row is None:
        print(f"[HARD] DB に {week_id} の行が存在しません。", file=sys.stderr)
        sys.exit(1)

    db_errors = verify_db_draft_matches_local(db_row, draft)
    if db_errors:
        print("[HARD] DB draft とローカル draft が一致しません:", file=sys.stderr)
        for e in db_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    pre_errors = verify_pre_publish(approval, draft, db_row)
    if pre_errors:
        print("[HARD] 公開前検証エラー:", file=sys.stderr)
        for e in pre_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    print("  DB 確認 OK")

    # 8. Pages 再検証（1 パスのみ）
    print("  Pages 再検証中...")
    teaser_path = PUBLIC_TEASER_DIR / f"{week_id}.json"
    if not teaser_path.exists():
        print(f"[HARD] ローカルの teaser ファイルが見つかりません: {teaser_path}", file=sys.stderr)
        sys.exit(1)

    with open(teaser_path, encoding="utf-8") as f:
        expected_teaser = json.load(f)

    expected_entry = build_index_entry(expected_teaser)
    pages_teaser_errors = verify_deployed_teaser(week_id, expected_teaser, retries=1, interval=0)
    pages_index_errors  = verify_deployed_index(week_id, expected_entry, retries=1, interval=0)

    if pages_teaser_errors or pages_index_errors:
        print("[WARN] Pages 再検証で問題が検出されました（処理は続行します）:")
        for e in pages_teaser_errors + pages_index_errors:
            print(f"  {e}")

    # 9. 事前情報表示
    sep = "=" * 60
    print(sep)
    print("  WEEKLY MARKETCAST — 公開完了処理プレビュー")
    print(sep)
    print(f"  week_id        : {week_id}")
    print(f"  revision       : {approval.get('revision')}")
    print(f"  published_at   : {pub_state.get('published_at')}")
    print(f"  OPERATOR_NAME  : {operator_name}")
    print(f"  approved_by    : {approval.get('approved_by')}")
    print(f"  teaser_hash    : {approval.get('teaser_hash', '')[:8]}...")
    print(f"  DB action      : draft → published")
    print(sep)

    # 10. dry-run はここで終了
    if dry_run:
        print("\ndry-run: DB 更新・アーカイブは行っていません。")
        sys.exit(0)

    # 11. 明示的確認入力（"PUBLISH YYYY-WXX" の完全一致）
    print(f"\nDB を published に遷移するには「PUBLISH {week_id}」と入力してください。")
    print("（キャンセルするには Enter を押してください）")
    try:
        user_input = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nキャンセルしました。")
        sys.exit(0)

    if not _parse_publish_input(user_input, week_id):
        print(f"入力が一致しませんでした（入力: {user_input!r}）。キャンセルしました。")
        sys.exit(0)

    # 12. DB を published に遷移（pub_state の published_at を使用）
    print("  DB published 化中...")
    published_at = pub_state["published_at"]
    publish_payload = build_publish_payload(approval, published_at)
    revision = approval["revision"]

    try:
        rows = db.patch_report_to_published(
            week_id, revision, publish_payload,
            allow_production=args.production,
        )
    except (WeeklyDBError, ProductionGuardError) as e:
        print(f"[HARD] DB 更新エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if len(rows) == 0:
        print(
            f"[HARD] PATCH で更新された行が 0 件でした。\n"
            f"  week_id={week_id}, revision={revision} の draft 行が存在するか確認してください。",
            file=sys.stderr,
        )
        sys.exit(1)
    if len(rows) > 1:
        print(f"[HARD] PATCH で複数行が更新されました（{len(rows)} 件）。", file=sys.stderr)
        sys.exit(1)

    db_published_row = rows[0]

    # 13. DB 整合確認
    mismatches = verify_published_report(db_published_row, publish_payload, approval)
    if mismatches:
        print("[HARD] DB published 整合確認失敗:", file=sys.stderr)
        for m in mismatches:
            print(f"  {m}", file=sys.stderr)
        sys.exit(1)
    print("  DB published OK")

    # 14. Pages / DB hash 照合
    db_teaser_hash = db_published_row.get("teaser_hash")
    pages_teaser_hash = expected_teaser.get("teaser_hash")
    if db_teaser_hash != pages_teaser_hash:
        print(
            f"[HARD] Pages teaser_hash と DB teaser_hash が不一致:\n"
            f"  Pages: {pages_teaser_hash!r}\n"
            f"  DB   : {db_teaser_hash!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    print("  hash 照合 OK")

    # 15. pub state を db_published に進める
    try:
        state_db = advance_stage(pub_state, "db_published")
    except PubStateError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)
    save_pub_state(state_db)

    # 16. draft アーカイブ
    print("  draft アーカイブ中...")
    draft_path_resolved = (
        args.draft_path
        if args.draft_path is not None
        else _DRAFTS_DIR / f"{week_id}_draft.json"
    )
    try:
        archive_path = archive_draft(draft_path_resolved, week_id)
    except (OSError, FileNotFoundError) as e:
        print(f"[HARD] アーカイブエラー: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"  archive: {archive_path}")

    # 17. pub state を completed に進める
    try:
        state_completed = advance_stage(state_db, "completed")
    except PubStateError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)
    saved = save_pub_state(state_completed)

    # 18. 完了表示
    print(f"\n[完了] {week_id} の公開が完了しました。")
    print(f"  DB status   : published")
    print(f"  published_at: {published_at}")
    print(f"  archive     : {archive_path}")
    print(f"  pub_state   : {saved} (stage=completed)")


if __name__ == "__main__":
    main()
