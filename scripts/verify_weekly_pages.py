#!/usr/bin/env python3
"""
verify_weekly_pages.py — GitHub Pages 公開ファイルの検証

git push 後に実行し、teaser と index が正しくデプロイされていることを確認する。
確認成功後、pub state を prepared → pages_verified に進める。

使用例:
  python scripts/verify_weekly_pages.py --week-id 2026-W26
  python scripts/verify_weekly_pages.py --week-id 2026-W26 --approval-path /path/to/approval.json

制約:
  - DB は変更しない
  - git commit/push は行わない
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from weekly_approval import load_approval, ApprovalFileError
from weekly_pages import (
    advance_stage,
    build_index_entry,
    load_pub_state,
    save_pub_state,
    verify_deployed_index,
    verify_deployed_teaser,
    PUBLIC_TEASER_DIR,
    PubStateError,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GitHub Pages 公開ファイルを検証し、pub state を pages_verified に進める"
    )
    parser.add_argument("--week-id",       required=True, help="例: 2026-W26")
    parser.add_argument("--approval-path", type=Path,     help="承認ファイルのパスを明示指定")
    args = parser.parse_args()

    week_id = args.week_id

    print(f"[W2-4B] Weekly Marketcast Pages 検証 — {week_id}")

    # 1. pub state 読み込み
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
    if stage == "pages_verified":
        print(f"[完了] 既に pages_verified 状態です。（冪等終了）")
        sys.exit(0)
    if stage != "prepared":
        print(
            f"[HARD] pub state のステージが 'prepared' ではありません: stage={stage!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    # 2. 承認ファイルから expected teaser を再構築
    try:
        approval = load_approval(week_id, path=args.approval_path)
    except ApprovalFileError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    if approval is None:
        print(f"[HARD] {week_id} の承認ファイルが見つかりません。", file=sys.stderr)
        sys.exit(1)

    # expected teaser（local ファイルから読む）
    teaser_path = PUBLIC_TEASER_DIR / f"{week_id}.json"
    if not teaser_path.exists():
        print(
            f"[HARD] ローカルの teaser ファイルが見つかりません: {teaser_path}\n"
            f"  prepare_weekly_publication.py を先に実行してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    import json
    with open(teaser_path, encoding="utf-8") as f:
        expected_teaser = json.load(f)

    expected_entry = build_index_entry(expected_teaser)

    # 3. teaser 検証
    print(f"  teaser 検証中（最大 5 回リトライ / 15 秒間隔）...")
    teaser_errors = verify_deployed_teaser(week_id, expected_teaser)
    if teaser_errors:
        print("[HARD] Pages teaser 検証失敗:", file=sys.stderr)
        for e in teaser_errors:
            print(f"  {e}", file=sys.stderr)
        print("\n  git push 後に時間をおいてから再試行してください。", file=sys.stderr)
        sys.exit(1)
    print("  teaser OK")

    # 4. index 検証
    print(f"  index 検証中...")
    index_errors = verify_deployed_index(week_id, expected_entry)
    if index_errors:
        print("[HARD] Pages index 検証失敗:", file=sys.stderr)
        for e in index_errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    print("  index OK")

    # 5. pub state を pages_verified に進める
    try:
        new_state = advance_stage(pub_state, "pages_verified")
    except PubStateError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    saved = save_pub_state(new_state)
    print(f"\n[完了] Pages 検証成功。pub state を pages_verified に更新しました。")
    print(f"  pub_state: {saved}")
    print(f"\n次のステップ:")
    print(f"  python scripts/finalize_weekly_publication.py --week-id {week_id}")


if __name__ == "__main__":
    main()
