"""
weekly_matcher.py — Deno Matcher / Timeline wrapper

matching.ts + timeline.ts を Deno subprocess で呼び出す Python ラッパー。
Python 側に Matcher ロジックを複製しない。

使用:
    from weekly_matcher import run_matcher, build_state_tags_from_context, MatcherError
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# プロジェクトルートからの相対パス
_PROJECT_ROOT    = Path(__file__).parent.parent
_WRAPPER_SCRIPT  = Path(__file__).parent / "run_weekly_matcher.ts"
_EVENT_REACTIONS = _PROJECT_ROOT / "data" / "event_reactions.json"
_CONTEXT_PUBLIC  = _PROJECT_ROOT / "data" / "current_context_public.json"

# 全 cause_tag リスト（event_reactions.json の cause_tag_labels から取得）
_ALL_CAUSE_TAGS_FALLBACK: list[str] = [
    "war", "terror", "supply_shock", "demand_shock",
    "monetary_tightening", "monetary_easing", "emergency_cut",
    "pandemic", "bank_crisis", "debt_crisis", "currency_crisis",
    "natural_disaster", "middle_east",
]


class MatcherError(Exception):
    pass


# ─── コンテキスト読み込み ────────────────────────────────────────────────────

def load_context(path: Path | None = None) -> dict:
    """
    current_context_public.json を読み込む。

    Returns: {"market_state_tags": {...}, "data_completeness": 3, "indicators": {...}, ...}
    """
    p = path or _CONTEXT_PUBLIC
    if not p.exists():
        raise MatcherError(
            f"current_context_public.json が見つかりません: {p}\n"
            "GitHub Actions の build 後に生成されるファイルです。"
        )
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def build_state_tags_from_context(context: dict) -> dict[str, str]:
    """
    context から market_state_tags を取得する。

    Args:
        context: load_context() の戻り値

    Returns:
        {"vix": "elev", "oil": "mid", "rate": "high"} など

    Raises:
        MatcherError: state_tags が取得できない場合
    """
    state_tags = context.get("market_state_tags")
    if not isinstance(state_tags, dict) or not state_tags:
        raise MatcherError(
            "current_context_public.json に有効な market_state_tags がありません"
        )
    return dict(state_tags)


def get_data_completeness(context: dict) -> int:
    """context から data_completeness を取得。デフォルト 0。"""
    v = context.get("data_completeness")
    return int(v) if isinstance(v, (int, float)) else 0


# ─── events 読み込み ─────────────────────────────────────────────────────────

def load_events(path: Path | None = None) -> tuple[list[dict], list[str]]:
    """
    event_reactions.json のイベント配列と cause_tag_labels を返す。

    Returns:
        (events, cause_tag_list)
    """
    p = path or _EVENT_REACTIONS
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    events = data.get("events", [])
    cause_tag_labels = list(data.get("cause_tag_labels", {}).keys())
    return events, cause_tag_labels


# ─── Deno 呼び出し ───────────────────────────────────────────────────────────

def _run_deno(
    cause_tag: str,
    state_tags: dict[str, str],
    events: list[dict],
    top_n: int = 5,
) -> list[dict]:
    """
    Deno で run_weekly_matcher.ts を実行し、matches リストを返す。

    Raises:
        MatcherError: 実行失敗・JSON 解析失敗・stdout 汚染
    """
    if not _WRAPPER_SCRIPT.exists():
        raise MatcherError(f"Deno wrapper が見つかりません: {_WRAPPER_SCRIPT}")

    payload = json.dumps(
        {"cause_tag": cause_tag, "state_tags": state_tags, "events": events, "top_n": top_n},
        ensure_ascii=False,
    )

    try:
        result = subprocess.run(
            ["deno", "run", "--allow-read", str(_WRAPPER_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    except FileNotFoundError:
        raise MatcherError("deno コマンドが見つかりません。Deno をインストールしてください。")
    except subprocess.TimeoutExpired:
        raise MatcherError("Deno 実行がタイムアウトしました（60秒）")

    # stderr はログとして出力（エラーでなくても）
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")

    if result.returncode != 0:
        raise MatcherError(
            f"Deno が終了コード {result.returncode} で終了しました\n"
            f"stderr: {result.stderr[:500]}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise MatcherError("Deno の stdout が空です")

    # stdout が JSON 以外の内容を含んでいないか確認（先頭文字チェック）
    if not stdout.startswith("{"):
        raise MatcherError(
            f"Deno stdout が JSON ではありません（先頭: {stdout[:50]!r}）\n"
            "stdout にログを混入させていないか確認してください。"
        )

    try:
        output = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise MatcherError(f"Deno 出力の JSON 解析失敗: {e}\nstdout: {stdout[:200]}")

    return output.get("matches", [])


# ─── 全 cause_tag で実行して上位 N 件を返す ──────────────────────────────────

def run_matcher(
    state_tags: dict[str, str],
    events: list[dict],
    cause_tags: list[str] | None = None,
    top_n: int = 5,
) -> list[dict]:
    """
    指定した cause_tags (省略時は全タグ) に対してマッチングを実行し、
    イベント単位で重複排除後、スコア上位 top_n 件を返す。

    同じ event_id が複数 cause_tag で出現した場合は最初の出現を採用
    （score は state_tags が同じなら identical のため）。

    Returns:
        list of match dicts (rank, event_id, event_name, event_date, score,
                             matched_axes, unmatched_axes, cause_tags,
                             why_reaction, key_insight, timelines)
    """
    tags = cause_tags if cause_tags is not None else _ALL_CAUSE_TAGS_FALLBACK

    seen_event_ids: set[str] = set()
    all_matches: list[dict] = []

    for tag in tags:
        try:
            matches = _run_deno(tag, state_tags, events, top_n)
        except MatcherError as e:
            print(f"[matcher] cause_tag={tag!r} 実行失敗: {e}", file=sys.stderr)
            continue

        for m in matches:
            eid = m.get("event_id")
            if eid and eid not in seen_event_ids:
                seen_event_ids.add(eid)
                all_matches.append(m)

    # スコア降順 → 日付降順でソート
    def _sort_key(m: dict) -> tuple:
        score = m.get("score", 0)
        date_int = _date_key(m.get("event_date", ""))
        return (-score, date_int)

    all_matches.sort(key=_sort_key)

    # rank を振り直す
    for i, m in enumerate(all_matches[:top_n]):
        m["rank"] = i + 1

    return all_matches[:top_n]


def _date_key(d: str) -> int:
    """ISO日付文字列を負の整数にする（新しい日付ほど小さい値）。"""
    try:
        return -int(d.replace("-", ""))
    except (ValueError, AttributeError):
        return 0


# ─── 単一 cause_tag 実行（parity test 用） ──────────────────────────────────

def run_matcher_single(
    cause_tag: str,
    state_tags: dict[str, str],
    events: list[dict],
    top_n: int = 5,
) -> list[dict]:
    """
    単一 cause_tag でマッチングを実行する（Edge Function の動作と完全一致）。
    parity test 用。
    """
    matches = _run_deno(cause_tag, state_tags, events, top_n)
    for i, m in enumerate(matches):
        m["rank"] = i + 1
    return matches
