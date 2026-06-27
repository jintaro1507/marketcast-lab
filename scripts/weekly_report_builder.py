"""
weekly_report_builder.py — Weekly Marketcast draft アセンブラ

free_teaser / paid_body の組み立て・検証・hash 計算を行う。
Matcher・Timeline のロジックはここに含めない。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import jsonschema

from weekly_config import ASSET_CONFIGS, ASSET_CONFIG_MAP, is_restricted
from weekly_dates import week_id_to_period
from weekly_templates import DISCLAIMER, generate_env_label, generate_summary, generate_observation_points
from weekly_themes import select_themes, load_group_metadata

# ─── プロジェクト設定 ─────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).parent.parent
_SCHEMA_FREE  = _PROJECT_ROOT / "schemas" / "weekly_free_teaser.schema.json"
_SCHEMA_PAID  = _PROJECT_ROOT / "schemas" / "weekly_paid_body.schema.json"

# ─── 水準分類（matching.ts と同一閾値） ──────────────────────────────────────

def classify_vix(value: float | None) -> str | None:
    """VIX 水準分類（matching.ts の classifyVix と同一）。"""
    if value is None:
        return None
    if value < 15:
        return "calm"
    if value < 25:
        return "elev"
    if value < 40:
        return "stress"
    return "panic"


def classify_oil(value: float | None) -> str | None:
    """WTI 水準分類（matching.ts の classifyOil と同一）。"""
    if value is None:
        return None
    if value < 40:
        return "lo"
    if value < 80:
        return "mid"
    return "hi"


# ─── 禁止キー（restricted leak check） ───────────────────────────────────────

FORBIDDEN_KEYS: frozenset[str] = frozenset({
    "value",
    "current_value",
    "previous_value",
    "latest_value",
    "raw_value",
    "price",
    "close",
    "api_key",
    "service_role_key",
    "authorization",
    "jwt",
})

# ─── 禁止表現 ─────────────────────────────────────────────────────────────────

FORBIDDEN_EXPRESSIONS: list[str] = [
    "予想", "見込", "買い時", "売り時", "投資妙味",
    "推奨", "可能性が高い", "必ず", "絶対",
    "すべき", "回復が期待", "上昇するだろう", "下落するだろう",
    "投資家は", "上昇が続", "下落圧力", "安全資産需要", "供給懸念",
    "買われる", "売られる",
]


# ─── asset_summaries 構築 ────────────────────────────────────────────────────

def build_asset_summaries(
    weekly_changes: dict,
    context: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """
    weekly_changes.assets から paid_body.asset_summaries を構築する。

    Args:
        weekly_changes: calculate_weekly_changes.py の出力 JSON
        context: current_context_public.json（VIX/WTI の水準分類に使用）

    Returns:
        (asset_summaries, hard_errors)

    制約:
      - 6件固定（ASSET_ORDER 順）
      - gold/sp500 の end_value は常に null / restricted=True
      - 他資産も W2-2 では end_value=null
      - ust10y のみ pt_change、他は pct_change
    """
    assets_in = {a["asset_key"]: a for a in weekly_changes.get("assets", [])}
    hard_errors: list[str] = []

    # context から VIX / WTI 水準値を取得
    indicators = (context or {}).get("indicators", {})
    vix_level = _indicator_value(indicators, "vix")
    oil_level = _indicator_value(indicators, "oil")

    summaries: list[dict] = []
    for cfg in ASSET_CONFIGS:
        key    = cfg["asset_key"]
        label  = cfg["label"]
        restr  = is_restricted(key)
        asset  = assets_in.get(key, {})

        direction  = asset.get("direction", "na")
        pct_change = None
        pt_change  = None

        if cfg["change_type"] == "pt":
            pt_change = asset.get("pt_change")
        else:
            pct_change = asset.get("pct_change")

        # direction が na の場合は変化率も null にする
        if direction == "na":
            pct_change = None
            pt_change  = None

        # level_class
        if key == "vix":
            level_class: str | None = classify_vix(vix_level)
        elif key == "wti":
            level_class = classify_oil(oil_level)
        else:
            level_class = None

        summaries.append({
            "asset_key":   key,
            "label":       label,
            "restricted":  restr,
            "direction":   direction,
            "pct_change":  None if restr else pct_change,  # restricted でも変化率は公開 OK
            "pt_change":   None if restr else pt_change,
            "level_class": level_class,
            "end_value":   None,  # W2-2 では全資産 null
        })

        # restricted でも pct_change は露出してよい（schema 上 allowed）
        # 再代入: restricted は end_value のみ null 強制
        if restr:
            # pct_change は公開（方向情報の一部）
            summaries[-1]["pct_change"] = pct_change
            summaries[-1]["pt_change"]  = pt_change

    if len(summaries) != 6:
        hard_errors.append(f"asset_summaries が6件でありません: {len(summaries)}件")

    return summaries, hard_errors


def _indicator_value(indicators: dict, key: str) -> float | None:
    """context.indicators から値を取得。status!=ok または値なし → None。"""
    ind = indicators.get(key, {})
    if ind.get("status") == "ok":
        v = ind.get("value")
        return float(v) if isinstance(v, (int, float)) else None
    return None


# ─── similar_events 変換 ─────────────────────────────────────────────────────

def build_similar_events(
    matches: list[dict],
    max_events: int = 3,
) -> tuple[list[dict], list[str]]:
    """
    Matcher 出力の matches から paid_body.similar_events を構築する。

    Args:
        matches: weekly_matcher.run_matcher() の戻り値
        max_events: 最大件数（デフォルト 3、schema 上 5 まで許容）

    Returns:
        (similar_events, warnings)
    """
    warnings: list[str] = []
    events: list[dict] = []

    for m in matches[:max_events]:
        timelines = _filter_timelines(m.get("timelines") or {})

        why_reaction = m.get("why_reaction") or None
        key_insight  = m.get("key_insight") or None

        if why_reaction is None:
            warnings.append(f"why_reaction 欠損: {m.get('event_id')}")
        if key_insight is None:
            warnings.append(f"key_insight 欠損: {m.get('event_id')}")

        events.append({
            "rank":          m["rank"],
            "event_id":      m["event_id"],
            "event_name":    m["event_name"],
            "event_date":    m["event_date"],
            "score":         m["score"],
            "matched_axes":  m.get("matched_axes", []),
            "unmatched_axes": m.get("unmatched_axes", []),
            "timelines":     timelines,
            "why_reaction":  why_reaction,
            "key_insight":   key_insight,
        })

    if len(events) < 3:
        warnings.append(f"類似局面が3件未満: {len(events)}件")

    return events, warnings


def _filter_timelines(
    raw: dict[str, Any],
) -> dict[str, dict]:
    """
    Matcher 出力の timelines を schema 準拠形式に変換する。
    方向文字列（up/down/flat/na）と mid_term_reversal のみ。
    """
    valid_dirs = {"up", "down", "flat", "na"}
    result: dict[str, dict] = {}

    for asset_key in ["wti", "gold", "sp500", "ust10y", "usdjpy", "vix"]:
        tl = raw.get(asset_key)
        if not isinstance(tl, dict):
            continue
        d1  = tl.get("d1",  "na")
        d7  = tl.get("d7",  "na")
        d30 = tl.get("d30", "na")
        d90 = tl.get("d90", "na")
        mtr = bool(tl.get("mid_term_reversal", False))

        # 不正な値は na に修正
        result[asset_key] = {
            "d1":  d1  if d1  in valid_dirs else "na",
            "d7":  d7  if d7  in valid_dirs else "na",
            "d30": d30 if d30 in valid_dirs else "na",
            "d90": d90 if d90 in valid_dirs else "na",
            "mid_term_reversal": mtr,
        }

    return result


# ─── タイトル生成 ─────────────────────────────────────────────────────────────

def build_title(week_id: str, period_start: str, period_end: str) -> str:
    """
    レポートタイトルを生成する。

    例: "Weekly Marketcast 2026年第25週（6/15〜6/19）"
    """
    import datetime

    try:
        year, week = _parse_week_id(week_id)
    except ValueError:
        return f"Weekly Marketcast {week_id}"

    start = datetime.date.fromisoformat(period_start)
    end   = datetime.date.fromisoformat(period_end)

    return (
        f"Weekly Marketcast {year}年第{week}週"
        f"（{start.month}/{start.day}〜{end.month}/{end.day}）"
    )


def _parse_week_id(week_id: str) -> tuple[int, int]:
    m = re.match(r"^(\d{4})-W(\d{1,2})$", week_id)
    if not m:
        raise ValueError(f"Invalid week_id: {week_id}")
    return int(m.group(1)), int(m.group(2))


# ─── free_teaser 構築 ────────────────────────────────────────────────────────

def build_free_teaser(
    week_id: str,
    period_start: str,
    period_end: str,
    asset_summaries: list[dict],
    free_theme: dict | None,
    similar_events: list[dict],
    summary: str,
    env_label: str,
) -> dict:
    """
    free_teaser を構築する。有料情報（score/Timeline/中期反転）を含まない。
    """
    top_match_preview: dict | None = None
    if similar_events:
        top = similar_events[0]
        top_match_preview = {
            "event_name": top["event_name"],
            "event_date": top["event_date"],
        }

    return {
        "week_id":     week_id,
        "title":       build_title(week_id, period_start, period_end),
        "period_start": period_start,
        "period_end":   period_end,
        "env_label":    env_label,
        "teaser_summary": summary,
        "featured_theme": free_theme,
        "top_match_preview": top_match_preview,
        "disclaimer": DISCLAIMER,
    }


# ─── paid_body 構築 ──────────────────────────────────────────────────────────

def build_paid_body(
    summary: str,
    asset_summaries: list[dict],
    paid_themes: list[dict],
    similar_events: list[dict],
    observation_points: list[str],
) -> dict:
    """paid_body を構築する。"""
    return {
        "summary":           summary,
        "asset_summaries":   asset_summaries,
        "themes":            paid_themes,
        "similar_events":    similar_events,
        "observation_points": observation_points,
        "disclaimer":        DISCLAIMER,
    }


# ─── restricted leak check ───────────────────────────────────────────────────

def check_restricted_leak(obj: Any, path: str = "") -> list[str]:
    """
    draft 全体を再帰的に探索し、禁止キーが存在すれば HARD エラーとして返す。

    end_value は schema 上存在するため除外（gold/sp500 の null 制約は schema 検証で確認）。
    """
    errors: list[str] = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            cur_path = f"{path}.{k}" if path else k
            if k.lower() in FORBIDDEN_KEYS:
                errors.append(f"[HARD] restricted キー検出: {cur_path!r}")
            else:
                errors.extend(check_restricted_leak(v, cur_path))

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            errors.extend(check_restricted_leak(item, f"{path}[{i}]"))

    return errors


# ─── free_teaser paid 情報混入チェック ───────────────────────────────────────

_PAID_KEYS_IN_FREE: frozenset[str] = frozenset({
    "score", "matched_axes", "unmatched_axes",
    "timelines", "mid_term_reversal",
    "why_reaction", "key_insight", "blurred",
    "asset_summaries", "similar_events", "observation_points",
    "end_value",
})


def check_free_teaser_leak(free_teaser: dict) -> list[str]:
    """
    free_teaser に有料情報（score・Timeline 等）が混入していないか確認。
    """
    errors: list[str] = []
    _recursive_key_check(free_teaser, _PAID_KEYS_IN_FREE, errors, "free_teaser")
    return errors


def _recursive_key_check(
    obj: Any,
    forbidden: frozenset[str],
    errors: list[str],
    path: str,
) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in forbidden:
                errors.append(f"[HARD] free_teaser に有料キー混入: {path}.{k!r}")
            else:
                _recursive_key_check(v, forbidden, errors, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _recursive_key_check(item, forbidden, errors, f"{path}[{i}]")


# ─── 禁止表現チェック ─────────────────────────────────────────────────────────

def check_forbidden_expressions(
    texts: list[str],
    source_label: str = "",
) -> list[str]:
    """
    テキストリストに禁止表現が含まれていないか確認する。

    group_metadata 由来の承認済み文言は改変しない。
    混入を検出した場合は HARD エラーとして返す。
    """
    errors: list[str] = []
    for i, text in enumerate(texts):
        for expr in FORBIDDEN_EXPRESSIONS:
            if expr in text:
                label = f"{source_label}[{i}]" if source_label else f"[{i}]"
                errors.append(f"[HARD] 禁止表現 {expr!r} を検出: {label}: {text[:60]!r}")
    return errors


# ─── hash 生成 ───────────────────────────────────────────────────────────────

def compute_hash(obj: dict) -> str:
    """
    SHA-256 ハッシュを生成する。

    規則:
      - JSON canonical 化（sort_keys=True, separators=(',',':'), ensure_ascii=False）
      - UTF-8 エンコード
      - lowercase hex 64文字
    """
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_hash_format(h: str) -> list[str]:
    """hash 形式を検証する（lowercase hex 64文字）。"""
    errors: list[str] = []
    if len(h) != 64:
        errors.append(f"[HARD] hash が64文字でありません: {len(h)}文字")
    if not re.match(r"^[0-9a-f]{64}$", h):
        errors.append(f"[HARD] hash に大文字または非 hex 文字が含まれています: {h[:20]!r}")
    return errors


# ─── JSON Schema 検証 ────────────────────────────────────────────────────────

def _load_schema(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_free_teaser(free_teaser: dict) -> list[str]:
    """weekly_free_teaser.schema.json に対して検証する。"""
    return _validate(free_teaser, _SCHEMA_FREE, "free_teaser")


def validate_paid_body(paid_body: dict) -> list[str]:
    """weekly_paid_body.schema.json に対して検証する。"""
    return _validate(paid_body, _SCHEMA_PAID, "paid_body")


def _validate(obj: dict, schema_path: Path, label: str) -> list[str]:
    schema = _load_schema(schema_path)
    errors: list[str] = []
    try:
        jsonschema.validate(obj, schema)
    except jsonschema.ValidationError as e:
        errors.append(f"[HARD] {label} schema 検証失敗: {e.message}")
    except jsonschema.SchemaError as e:
        errors.append(f"[HARD] schema ファイル不正: {e.message}")
    return errors


# ─── draft 構築（メインエントリ） ────────────────────────────────────────────

def build_draft(
    weekly_changes: dict,
    matches: list[dict],
    context: dict | None = None,
    group_metadata_path: Path | None = None,
) -> tuple[dict, list[str], list[str]]:
    """
    週次 draft を完全に構築する。

    Returns:
        (draft_dict, warnings, hard_errors)
    """
    warnings:    list[str] = []
    hard_errors: list[str] = []

    week_id      = weekly_changes["week_id"]
    period_start = weekly_changes["period_start"]
    period_end   = weekly_changes["period_end"]

    # 1. asset_summaries
    asset_summaries, asset_hard = build_asset_summaries(weekly_changes, context)
    hard_errors.extend(asset_hard)

    # 2. similar_events
    similar_events, match_warns = build_similar_events(matches, max_events=3)
    warnings.extend(match_warns)

    if not similar_events:
        hard_errors.append("[HARD] Matcher 結果が0件です")

    # 3. テーマ選定（schema 変換前の matches から cause_tags を使う）
    group_metadata = load_group_metadata(group_metadata_path)
    paid_themes, free_theme, theme_warns = select_themes(
        matches, group_metadata  # matches には cause_tags フィールドが含まれる
    )

    for wt in theme_warns:
        warnings.append(f"group_metadata の caveat 欠損: {wt}")

    if not paid_themes:
        hard_errors.append("[HARD] 有効なテーマが0件です（group_metadata に有効なテーマなし）")

    # 4. 要約・環境ラベル・観測ポイント
    summary            = generate_summary(asset_summaries)
    env_label          = generate_env_label(asset_summaries)
    observation_points = generate_observation_points(asset_summaries, similar_events)

    # 5. free_teaser
    free_teaser = build_free_teaser(
        week_id, period_start, period_end,
        asset_summaries, free_theme, similar_events,
        summary, env_label,
    )

    # 6. paid_body
    paid_body = build_paid_body(
        summary, asset_summaries, paid_themes,
        similar_events, observation_points,
    )

    # 7. restricted leak check
    hard_errors.extend(check_restricted_leak(free_teaser, "free_teaser"))
    hard_errors.extend(check_restricted_leak(paid_body,   "paid_body"))
    hard_errors.extend(check_free_teaser_leak(free_teaser))

    # 8. 禁止表現チェック
    # 生成テキスト（summary・observation_points）→ HARD
    generated_texts = [summary] + observation_points
    hard_errors.extend(check_forbidden_expressions(generated_texts, "generated"))

    # 承認済みメタデータ文言（themes.summary・caveat）→ WARN（勝手に改変しない）
    metadata_texts = (
        [t.get("summary", "") for t in paid_themes]
        + [t.get("caveat", "") for t in paid_themes]
    )
    for i, text in enumerate(metadata_texts):
        for expr in FORBIDDEN_EXPRESSIONS:
            if expr in text:
                warnings.append(
                    f"[WARN] group_metadata 承認済み文言に禁止表現 {expr!r}: {text[:60]!r}"
                )

    # 9. Schema 検証
    hard_errors.extend(validate_free_teaser(free_teaser))
    hard_errors.extend(validate_paid_body(paid_body))

    # 10. hash 生成（restricted 生値が含まれていないことを確認してから）
    teaser_hash   = ""
    paid_hash     = ""
    if not hard_errors:
        teaser_hash = compute_hash(free_teaser)
        paid_hash   = compute_hash(paid_body)
        hard_errors.extend(validate_hash_format(teaser_hash))
        hard_errors.extend(validate_hash_format(paid_hash))

    import datetime
    draft = {
        "week_id":       week_id,
        "revision":      1,
        "generated_at":  datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "free_teaser":   free_teaser,
        "paid_body":     paid_body,
        "teaser_hash":   teaser_hash,
        "paid_body_hash": paid_hash,
        "warnings":      warnings,
        "hard_errors":   hard_errors,
    }

    return draft, warnings, hard_errors
