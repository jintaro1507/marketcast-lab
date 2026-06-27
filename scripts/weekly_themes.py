"""
weekly_themes.py — 注目テーマ選定

Matcher 上位3件の cause_tags を集計し、group_metadata.json から
承認済みテーマを選定する純粋関数群。自由文生成なし。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# group_metadata.json のデフォルトパス
_DEFAULT_GROUP_METADATA = Path(__file__).parent.parent / "data" / "group_metadata.json"

# group_metadata.json 内のキー順（安定したタイブレーク順）
_STABLE_ORDER: list[str] = [
    "middle_east",
    "war",
    "monetary_easing",
    "monetary_tightening",
    "supply_shock",
    "emergency_cut",
    "pandemic",
    "terror",
    "demand_shock",
    "bank_crisis",
    "debt_crisis",
    "currency_crisis",
    "natural_disaster",
    "liquidity_crisis",
]


def load_group_metadata(path: Path | None = None) -> dict:
    """group_metadata.json を読み込む。"""
    p = path or _DEFAULT_GROUP_METADATA
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _stable_index(tag: str) -> int:
    """タイブレーク用の固定順序インデックス。未知タグは末尾。"""
    try:
        return _STABLE_ORDER.index(tag)
    except ValueError:
        return len(_STABLE_ORDER)


def _is_valid_for_paid(group: dict) -> bool:
    """paid_body テーマとして使用可能か（summary・caveat 両方が非空文字列）。"""
    summary = group.get("summary", "")
    caveat  = group.get("interpretation_caveat", "")
    return bool(summary and summary.strip()) and bool(caveat and caveat.strip())


def _is_valid_for_free(group: dict) -> bool:
    """free_teaser テーマとして使用可能か（summary が非空文字列）。"""
    summary = group.get("summary", "")
    return bool(summary and summary.strip())


def select_themes(
    similar_events: list[dict],
    group_metadata: dict,
    max_paid: int = 3,
) -> tuple[list[dict], dict | None, list[str]]:
    """
    similar_events の cause_tags を集計し、テーマを選定する。

    Returns:
        paid_themes   : paid_body 用テーマリスト（最大3件）
        free_theme    : free_teaser 用テーマ（最大1件）または None
        warn_tags     : WARN として報告するタグ（caveat 欠損など）

    ルール:
      1. similar_events（最大3件）の cause_tags を頻度集計
      2. 頻度順、同率は _STABLE_ORDER で安定ソート
      3. group_metadata に存在するものだけ
      4. paid_themes: summary + caveat 両方必須（max_paid 件）
      5. free_theme: summary のみ必須（top 1）
      6. テーマ0件は呼び出し元で HARD として扱う
    """
    groups = group_metadata.get("groups", {})
    warn_tags: list[str] = []

    # cause_tags 頻度集計（similar_events 最大3件）
    freq: dict[str, int] = {}
    for ev in similar_events[:3]:
        for tag in (ev.get("cause_tags") or []):
            freq[tag] = freq.get(tag, 0) + 1

    # 頻度順 → 安定順でソート
    sorted_tags: list[str] = sorted(
        freq.keys(),
        key=lambda t: (-freq[t], _stable_index(t)),
    )

    # paid テーマ選定
    paid_themes: list[dict] = []
    free_theme_candidate: dict | None = None

    for tag in sorted_tags:
        if tag not in groups:
            continue
        group = groups[tag]

        # free テーマ候補（先頭のみ）
        if free_theme_candidate is None and _is_valid_for_free(group):
            free_theme_candidate = {
                "tag":     tag,
                "label":   group.get("label", tag),
                "summary": group["summary"].strip(),
            }

        # paid テーマ
        if len(paid_themes) < max_paid:
            if _is_valid_for_paid(group):
                paid_themes.append({
                    "tag":     tag,
                    "label":   group.get("label", tag),
                    "summary": group["summary"].strip(),
                    "caveat":  group["interpretation_caveat"].strip(),
                })
            elif _is_valid_for_free(group):
                # summary はあるが caveat が空 → WARN
                warn_tags.append(tag)

    return paid_themes, free_theme_candidate, warn_tags
