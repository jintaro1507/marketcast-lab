#!/usr/bin/env python3
"""
generate_weekly_draft.py — Weekly Marketcast ルールベース完成稿 draft 生成

使用例:
  python scripts/generate_weekly_draft.py --week-id 2026-W25
  python scripts/generate_weekly_draft.py --week-id 2026-W25 --dry-run
  python scripts/generate_weekly_draft.py --week-id 2026-W25 --input-json /tmp/changes.json

処理フロー:
  1. week_id 検証
  2. W2-1 差分 JSON 取得（--input-json または ~/.local/share 配下から）
  3. current_context_public.json 読み込み（state_tags・水準値）
  4. Deno Matcher 実行（matching.ts + timeline.ts 再利用）
  5. テーマ選定
  6. 要約・環境ラベル・観測ポイント生成
  7. free_teaser / paid_body 構築
  8. restricted leak check / 禁止表現チェック
  9. JSON Schema 検証
  10. hash 計算
  11. draft プレビュー
  12. 保存確認（--dry-run のとき省略）
  13. draft 保存（~/.local/share/marketcast-lab/drafts/YYYY-WXX_draft.json）

制約:
  - 本番 Supabase にアクセスしない
  - weekly_reports に書き込まない
  - HARD が1件でも存在するとき保存しない
  - --dry-run では保存しない
  - 同一 week_id の draft が存在するとき上書き前に確認
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from weekly_dates import week_id_to_period, week_id_exists
from weekly_matcher import (
    load_context,
    load_events,
    build_state_tags_from_context,
    get_data_completeness,
    run_matcher,
    MatcherError,
)
from weekly_report_builder import build_draft

# ─── パス定義 ────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).parent.parent
_DRAFTS_DIR   = Path.home() / ".local" / "share" / "marketcast-lab" / "drafts"
_CHANGES_DIR  = Path.home() / ".local" / "share" / "marketcast-lab" / "changes"


# ─── 差分 JSON 取得 ───────────────────────────────────────────────────────────

def find_changes_json(week_id: str) -> Path | None:
    """
    週次差分 JSON のデフォルト保存先から取得する。

    保存先候補:
      ~/.local/share/marketcast-lab/changes/YYYY-WXX_changes.json
    """
    candidate = _CHANGES_DIR / f"{week_id}_changes.json"
    if candidate.exists():
        return candidate
    return None


def load_weekly_changes(week_id: str, input_json: Path | None) -> dict:
    """
    週次差分 JSON を読み込む。

    Args:
        week_id:    対象週 ID
        input_json: --input-json で指定されたパス（省略時はデフォルト保存先）

    Raises:
        SystemExit: ファイルが見つからない場合
    """
    if input_json:
        path = input_json
    else:
        path = find_changes_json(week_id)
        if path is None:
            print(
                f"[HARD] 週次差分 JSON が見つかりません: {week_id}\n"
                f"  探索場所: {_CHANGES_DIR / f'{week_id}_changes.json'}\n"
                f"  --input-json <path> で直接指定してください。",
                file=sys.stderr,
            )
            sys.exit(1)

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[HARD] 差分 JSON 読み込み失敗: {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if data.get("week_id") and data["week_id"] != week_id:
        print(
            f"[HARD] week_id が一致しません: ファイル={data['week_id']!r}, 引数={week_id!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    return data


# ─── draft 保存 ──────────────────────────────────────────────────────────────

def draft_path(week_id: str) -> Path:
    return _DRAFTS_DIR / f"{week_id}_draft.json"


def save_draft(week_id: str, draft: dict, dry_run: bool) -> None:
    """
    draft を保存する。

    - --dry-run のとき: 保存しない（プレビューのみ）
    - HARD が存在するとき: 保存しない（呼び出し元で確認済み）
    - 既存 draft が存在するとき: 上書き前に確認
    - directory: 700 / file: 600
    """
    if dry_run:
        print("\n[dry-run] draft 保存はスキップされました。")
        return

    _DRAFTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    _DRAFTS_DIR.chmod(0o700)

    path = draft_path(week_id)

    if path.exists():
        answer = input(f"\n既存の draft を上書きしますか？\n  {path}\n  [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("保存をキャンセルしました。")
            return

    out = json.dumps(draft, ensure_ascii=False, indent=2)
    path.write_text(out, encoding="utf-8")
    path.chmod(0o600)

    print(f"\n[保存完了] {path}")
    print(f"  権限: {oct(path.stat().st_mode & 0o777)}")


# ─── プレビュー表示 ───────────────────────────────────────────────────────────

def print_preview(draft: dict) -> None:
    """draft の主要フィールドをコンソールに表示する。"""
    print("\n" + "=" * 60)
    print("  WEEKLY MARKETCAST DRAFT プレビュー")
    print("=" * 60)

    ft = draft.get("free_teaser", {})
    pb = draft.get("paid_body", {})

    print(f"\n■ week_id    : {draft.get('week_id')}")
    print(f"  generated  : {draft.get('generated_at')}")
    print(f"\n■ タイトル    : {ft.get('title')}")
    print(f"  env_label  : {ft.get('env_label')}")
    print(f"  teaser要約  : {ft.get('teaser_summary')}")

    featured = ft.get("featured_theme")
    if featured:
        print(f"  注目テーマ  : [{featured['tag']}] {featured['label']}")

    top_match = ft.get("top_match_preview")
    if top_match:
        print(f"  top match  : {top_match.get('event_name')} ({top_match.get('event_date')})")

    print(f"\n■ 要約 (paid) : {pb.get('summary')}")

    print("\n■ 6資産まとめ :")
    for a in pb.get("asset_summaries", []):
        chg = a.get("pct_change") or a.get("pt_change")
        chg_txt = f"{chg:+.2f}" if chg is not None else "–"
        print(f"  {a['asset_key']:8s} dir={a['direction']:4s} chg={chg_txt}  lvl={a.get('level_class') or '–'}")

    themes = pb.get("themes", [])
    print(f"\n■ テーマ ({len(themes)}件) :")
    for t in themes:
        print(f"  [{t['tag']}] {t['label']}")

    events = pb.get("similar_events", [])
    print(f"\n■ 類似局面 ({len(events)}件) :")
    for ev in events:
        print(f"  #{ev['rank']} {ev['event_name']} ({ev['event_date']}) score={ev['score']}")

    obs = pb.get("observation_points", [])
    print(f"\n■ 観測ポイント ({len(obs)}件) :")
    for i, o in enumerate(obs, 1):
        print(f"  {i}. {o}")

    print(f"\n■ teaser_hash   : {draft.get('teaser_hash', '')[:16]}...")
    print(f"  paid_body_hash: {draft.get('paid_body_hash', '')[:16]}...")

    warnings    = draft.get("warnings", [])
    hard_errors = draft.get("hard_errors", [])

    if hard_errors:
        print(f"\n🔴 HARD エラー ({len(hard_errors)}件):")
        for e in hard_errors:
            print(f"  {e}")

    if warnings:
        print(f"\n🟡 WARN ({len(warnings)}件):")
        for w in warnings:
            print(f"  {w}")

    print("\n" + "=" * 60)


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weekly Marketcast draft を生成する（ルールベース）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python scripts/generate_weekly_draft.py --week-id 2026-W25
  python scripts/generate_weekly_draft.py --week-id 2026-W25 --dry-run
  python scripts/generate_weekly_draft.py --week-id 2026-W25 --input-json /tmp/changes.json

出力先:
  ~/.local/share/marketcast-lab/drafts/YYYY-WXX_draft.json
  (directory: 700, file: 600)
        """,
    )
    parser.add_argument("--week-id", required=True, help="対象週 ID (例: 2026-W25)")
    parser.add_argument("--input-json", type=Path, default=None,
                        help="W2-1 差分 JSON のパス（テスト・開発用）")
    parser.add_argument("--dry-run", action="store_true",
                        help="保存せずにプレビューのみ表示する")
    parser.add_argument("--context-json", type=Path, default=None,
                        help="current_context_public.json のパス（デフォルト: data/current_context_public.json）")
    args = parser.parse_args()

    week_id   = args.week_id
    dry_run   = args.dry_run
    input_json = args.input_json

    print(f"[W2-2] Weekly Marketcast draft 生成 — {week_id}")
    if dry_run:
        print("  [dry-run モード] 保存はスキップ")

    # ── 1. week_id 検証 ──────────────────────────────────────────────────────
    if not re.match(r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$", week_id):
        print(f"[HARD] week_id 形式が不正です: {week_id!r}", file=sys.stderr)
        sys.exit(1)

    if not week_id_exists(week_id):
        print(f"[WARN] week_id が存在しない可能性があります: {week_id!r}", file=sys.stderr)

    # ── 2. W2-1 差分 JSON 取得 ───────────────────────────────────────────────
    print(f"  差分 JSON 読み込み中...")
    changes = load_weekly_changes(week_id, input_json)

    # 入力の hard_errors を継承
    input_hard = changes.get("hard_errors", [])
    if input_hard:
        print(f"\n[HARD] 入力 JSON に hard_errors があります:")
        for e in input_hard:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    input_warns = changes.get("warnings", [])
    if input_warns:
        print(f"[WARN] 入力 JSON に warnings があります:")
        for w in input_warns:
            print(f"  {w}")

    # ── 3. current_context_public.json 読み込み ──────────────────────────────
    print(f"  context 読み込み中...")
    try:
        context = load_context(args.context_json)
    except MatcherError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    try:
        state_tags = build_state_tags_from_context(context)
    except MatcherError as e:
        print(f"[HARD] {e}", file=sys.stderr)
        sys.exit(1)

    data_completeness = get_data_completeness(context)
    if data_completeness < 2:
        print(
            f"[HARD] data_completeness が不足しています: {data_completeness} (最低2が必要)",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"  state_tags: {state_tags}")

    # ── 4. events 読み込み ──────────────────────────────────────────────────
    print(f"  event_reactions.json 読み込み中...")
    events, cause_tags = load_events()
    print(f"  {len(events)} イベント, {len(cause_tags)} cause_tags")

    # ── 5. Matcher 実行 ─────────────────────────────────────────────────────
    print(f"  Matcher 実行中（全 cause_tags: {len(cause_tags)} タグ）...")
    try:
        matches = run_matcher(state_tags, events, cause_tags, top_n=5)
    except MatcherError as e:
        print(f"[HARD] Matcher 実行失敗: {e}", file=sys.stderr)
        sys.exit(1)

    if not matches:
        print("[HARD] Matcher 結果が0件です", file=sys.stderr)
        sys.exit(1)

    print(f"  マッチ結果: {len(matches)} 件")
    for m in matches[:3]:
        print(f"    #{m['rank']} {m['event_name']} ({m['event_date']}) score={m['score']}")

    # ── 6-10. draft 構築 ─────────────────────────────────────────────────────
    print(f"\n  draft 構築中...")
    draft, warnings, hard_errors = build_draft(changes, matches, context)

    # 入力 warnings を追加
    for w in input_warns:
        if w not in warnings:
            warnings.append(w)
    draft["warnings"] = warnings

    # ── 11. プレビュー ───────────────────────────────────────────────────────
    print_preview(draft)

    # ── HARD チェック ────────────────────────────────────────────────────────
    if hard_errors:
        print(
            f"\n[HARD] {len(hard_errors)} 件の HARD エラーがあります。draft は保存されません。",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── 12-13. 保存 ──────────────────────────────────────────────────────────
    save_draft(week_id, draft, dry_run)

    if not dry_run and not hard_errors:
        print("\n[完了] W2-2 draft 生成が完了しました。")
        print(f"  次のステップ: draft レビュー → weekly_reports への保存（W2-3 以降）")


if __name__ == "__main__":
    main()
