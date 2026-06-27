"""
weekly_templates.py — ルールベーステンプレート生成

免責・要約・環境ラベル・観測ポイントを生成する純粋関数群。
AI API 不使用。同じ入力→同じ出力のdeterministic実装。

禁止表現（チェック対象と同一）:
  予想・見込・買い時・売り時・投資妙味・推奨・可能性が高い・必ず・絶対
  〜すべき・回復が期待・上昇するだろう・下落するだろう
"""
from __future__ import annotations

from typing import Any


# ─── 定数 ──────────────────────────────────────────────────────────────────

DISCLAIMER = (
    "本情報は過去の市場価格変化の記録であり、投資助言・売買推奨・将来の値動き予測ではありません。"
    "投資の意思決定はご自身の責任でご判断ください。"
)

# 資産ラベル（数値挿入用）
_ASSET_LABEL: dict[str, str] = {
    "wti":    "WTI原油先物",
    "gold":   "金",
    "sp500":  "S&P500",
    "ust10y": "米10年債利回り",
    "usdjpy": "ドル円",
    "vix":    "VIX（恐怖指数）",
}


# ─── 補助関数 ───────────────────────────────────────────────────────────────

def _pct_str(v: float | None, sign: bool = True) -> str:
    """変化率を "+3.2%" 形式の文字列にする。None → "–"。"""
    if v is None:
        return "–"
    prefix = "+" if sign and v > 0 else ""
    return f"{prefix}{v:.1f}%"


def _pt_str(v: float | None, sign: bool = True) -> str:
    """pt 変化を "+0.09pt" 形式の文字列にする。None → "–"。"""
    if v is None:
        return "–"
    prefix = "+" if sign and v > 0 else ""
    return f"{prefix}{v:.2f}pt"


def _get_asset(assets: list[dict], key: str) -> dict:
    """assets リストから asset_key で取得。なければ空 dict を返す。"""
    for a in assets:
        if a.get("asset_key") == key:
            return a
    return {}


def _direction(assets: list[dict], key: str) -> str:
    return _get_asset(assets, key).get("direction", "na")


def _pct(assets: list[dict], key: str) -> float | None:
    return _get_asset(assets, key).get("pct_change")


def _pt(assets: list[dict], key: str) -> float | None:
    return _get_asset(assets, key).get("pt_change")


def _level_class(assets: list[dict], key: str) -> str | None:
    return _get_asset(assets, key).get("level_class")


def _missing_count(assets: list[dict]) -> int:
    return sum(1 for a in assets if a.get("direction") == "na")


# ─── env_label 生成 ─────────────────────────────────────────────────────────

def generate_env_label(assets: list[dict]) -> str:
    """
    市場環境ラベルを生成する（50字以内、最大2要素、原因推測なし）。

    優先順位（固定ルール）:
      1. VIX panic        → "VIX急騰"
      2. VIX stress + sp500 down  → "VIX警戒・株式下落"
      3. VIX stress + sp500 up    → "VIX警戒・株式上昇"
      4. VIX stress               → "VIX警戒"
      5. gold up + sp500 down     → "安全資産優位"
      6. sp500 large up (pct≥2)  → "株式優位"
      7. wti large move (|pct|≥10) → "原油変動拡大"
      8. ust10y up + usdjpy up   → "金利上昇・ドル高"
      9. ust10y down             → "金利低下"
     10. all small (missing≥3)   → "一部データ欠損"
     11. all directions na/flat  → "小動き"
     12. default                 → "方向感分散"
    """
    vix_lc    = _level_class(assets, "vix")
    sp500_dir = _direction(assets, "sp500")
    gold_dir  = _direction(assets, "gold")
    wti_pct   = _pct(assets, "wti")
    sp500_pct = _pct(assets, "sp500")
    ust10y_dir = _direction(assets, "ust10y")
    usdjpy_dir = _direction(assets, "usdjpy")

    # 1. VIX panic
    if vix_lc == "panic":
        return "VIX急騰"

    # 2-4. VIX stress
    if vix_lc == "stress":
        if sp500_dir == "down":
            return "VIX警戒・株式下落"
        if sp500_dir == "up":
            return "VIX警戒・株式上昇"
        return "VIX警戒"

    # 5. gold up + sp500 down
    if gold_dir == "up" and sp500_dir == "down":
        return "安全資産優位"

    # 6. sp500 large up
    if sp500_pct is not None and sp500_pct >= 2.0:
        return "株式優位"

    # 7. WTI large move
    if wti_pct is not None and abs(wti_pct) >= 10.0:
        return "原油変動拡大"

    # 8. ust10y up + usdjpy up
    if ust10y_dir == "up" and usdjpy_dir == "up":
        return "金利上昇・ドル高"

    # 9. ust10y down
    if ust10y_dir == "down":
        return "金利低下"

    # 10. missing data
    if _missing_count(assets) >= 3:
        return "一部データ欠損"

    # 11. all small
    directions = [_direction(assets, k) for k in ["wti", "gold", "sp500", "ust10y", "usdjpy", "vix"]]
    if all(d in ("flat", "na") for d in directions):
        return "小動き"

    # 12. default
    return "方向感分散"


# ─── 要約テンプレート生成 ────────────────────────────────────────────────────

def generate_summary(assets: list[dict], max_chars: int = 200) -> str:
    """
    週次要約文を生成する（最大200字、最大3文程度）。

    優先順位（固定ルール）:
      1. VIX panic    → VIX急騰文
      2. VIX stress   → VIXやや高い文
      3. gold up + sp500 down  → 安全資産分散文
      4. sp500 large up        → S&P500上昇文
      5. wti large move        → 原油変動文
      6. ust10y up             → 金利上昇文
      7. ust10y down           → 金利低下文
      8. ust10y + usdjpy same  → 金利ドル同方向文
      9. missing≥4             → 欠損文
     10. all flat/na           → 小動き文
     11. default               → 方向感分散文

    追加文: ust10y・sp500 の方向は最大1文ずつ追記。
    文の重複は起こさない。
    """
    vix_lc    = _level_class(assets, "vix")
    vix_pct   = _pct(assets, "vix")
    gold_dir  = _direction(assets, "gold")
    gold_pct  = _pct(assets, "gold")
    sp500_dir = _direction(assets, "sp500")
    sp500_pct = _pct(assets, "sp500")
    wti_dir   = _direction(assets, "wti")
    wti_pct   = _pct(assets, "wti")
    ust10y_dir = _direction(assets, "ust10y")
    ust10y_pt  = _pt(assets, "ust10y")
    usdjpy_dir = _direction(assets, "usdjpy")

    sentences: list[str] = []

    # === 主文（優先順位 1〜11） ===
    added_sp500  = False
    added_ust10y = False

    # 1. VIX panic
    if vix_lc == "panic":
        sentences.append(
            f"VIX（恐怖指数）が急騰し、市場のストレス水準が極めて高い状態でした。"
        )
        if vix_pct is not None:
            sentences[-1] = (
                f"VIX（恐怖指数）が{_pct_str(vix_pct)}急騰し、市場のストレス水準が極めて高い状態でした。"
            )

    # 2. VIX stress
    elif vix_lc == "stress":
        sentences.append("VIXはやや高い水準で推移しました。")

    # 3. gold up + sp500 down
    if gold_dir == "up" and sp500_dir == "down":
        gold_txt  = f"金が{_pct_str(gold_pct)}上昇"
        sp500_txt = f"S&P500が{_pct_str(sp500_pct)}下落"
        sentences.append(f"{gold_txt}し、{sp500_txt}しました。")
        added_sp500 = True

    # 4. sp500 large up（gold up + sp500 down の場合は追加しない）
    elif sp500_pct is not None and sp500_pct >= 2.0 and not added_sp500:
        sentences.append(f"S&P500が{_pct_str(sp500_pct)}上昇しました。")
        added_sp500 = True

    # 5. WTI large move
    if wti_pct is not None and abs(wti_pct) >= 10.0:
        direction_txt = "上昇" if wti_dir == "up" else "下落" if wti_dir == "down" else "変動"
        sentences.append(f"WTI原油先物が{_pct_str(wti_pct)}{direction_txt}しました。")

    # 6. ust10y up
    if ust10y_dir == "up" and not added_ust10y:
        sentences.append(f"米10年債利回りが{_pt_str(ust10y_pt)}上昇しました。")
        added_ust10y = True

    # 7. ust10y down
    elif ust10y_dir == "down" and not added_ust10y:
        sentences.append(f"米10年債利回りが{_pt_str(ust10y_pt)}低下しました。")
        added_ust10y = True

    # 8. ust10y and usdjpy same direction（追記がない場合のみ）
    if not added_ust10y and ust10y_dir in ("up", "down") and ust10y_dir == usdjpy_dir:
        direction_txt = "上昇" if ust10y_dir == "up" else "低下"
        sentences.append(f"米10年債利回りとドル円が同方向（{direction_txt}）に動きました。")
        added_ust10y = True

    # 9. missing data dominant
    missing = _missing_count(assets)
    if missing >= 4:
        sentences.insert(0, f"一部資産のデータが取得できていません（{missing}資産が欠損）。")

    # 10. all small / flat — 主文がまだない場合
    elif not sentences:
        directions = [_direction(assets, k) for k in ["wti", "gold", "sp500", "ust10y", "usdjpy", "vix"]]
        if all(d in ("flat", "na") for d in directions):
            sentences.append("主要資産はいずれも小幅な変動にとどまりました。")
        else:
            # 11. default
            sentences.append("各資産の方向感が分かれた週となりました。")

    # === 追加文（sp500 / ust10y まだ未追記かつ 3文未満） ===
    if not added_sp500 and sp500_dir not in ("na", "flat") and sp500_pct is not None and len(sentences) < 3:
        direction_txt = "上昇" if sp500_dir == "up" else "下落"
        sentences.append(f"S&P500が{_pct_str(sp500_pct)}{direction_txt}しました。")

    if not added_ust10y and ust10y_dir not in ("na", "flat") and len(sentences) < 3:
        direction_txt = "上昇" if ust10y_dir == "up" else "低下"
        sentences.append(f"米10年債利回りが{_pt_str(ust10y_pt)}{direction_txt}しました。")

    result = "".join(sentences)
    # 200字超の場合は文単位でトリム（末尾から削除）
    while len(result) > max_chars and len(sentences) > 1:
        sentences.pop()
        result = "".join(sentences)

    return result or "市場データを確認中です。"


# ─── 観測ポイント生成 ────────────────────────────────────────────────────────

def generate_observation_points(
    assets: list[dict],
    similar_events: list[dict],
) -> list[str]:
    """
    来週の観測ポイントを 3〜5 件生成する（中立表現・原因解釈なし）。

    固定生成（必ず3件）:
      1. VIX
      2. WTI
      3. UST10Y

    条件付き追加（最大2件）:
      4. gold と sp500 の方向差（異なる方向のとき）
      5. usdjpy が large move のとき / または mid_term_reversal が見られたとき
    """
    points: list[str] = []

    vix_dir  = _direction(assets, "vix")
    wti_pct  = _pct(assets, "wti")
    ust10y_pt = _pt(assets, "ust10y")
    gold_dir = _direction(assets, "gold")
    sp500_dir = _direction(assets, "sp500")
    usdjpy_pct = _pct(assets, "usdjpy")

    # 1. VIX（常時）
    points.append("VIXの水準が前週から変化するかを確認する")

    # 2. WTI（常時）
    wti_note = ""
    if wti_pct is not None:
        wti_note = f"（今週{_pct_str(wti_pct)}）"
    points.append(f"WTI原油の方向と変動幅を確認する{wti_note}")

    # 3. UST10Y（常時）
    ust10y_note = ""
    if ust10y_pt is not None:
        ust10y_note = f"（今週{_pt_str(ust10y_pt)}）"
    points.append(f"米10年債利回りの方向が変化するかを確認する{ust10y_note}")

    # 4. gold vs sp500 方向差
    if (
        gold_dir not in ("na", "flat")
        and sp500_dir not in ("na", "flat")
        and gold_dir != sp500_dir
    ):
        gold_txt  = "上昇" if gold_dir == "up" else "下落"
        sp500_txt = "下落" if sp500_dir == "down" else "上昇"
        points.append(
            f"金と S&P500 の方向差が継続するかを確認する"
            f"（今週: 金{gold_txt}・S&P500{sp500_txt}）"
        )

    # 5. usdjpy large move または mid_term_reversal
    if len(points) < 5:
        # 類似局面で mid_term_reversal が見られた資産を収集
        reversal_assets: set[str] = set()
        for ev in (similar_events or []):
            for asset_key, tl in (ev.get("timelines") or {}).items():
                if tl.get("mid_term_reversal"):
                    reversal_assets.add(asset_key)

        if reversal_assets:
            asset_labels = [_ASSET_LABEL.get(k, k) for k in sorted(reversal_assets)]
            label_str = "・".join(asset_labels)
            points.append(
                f"類似局面で中期反転が見られた資産の方向を継続観測する（{label_str}）"
            )
        elif usdjpy_pct is not None and abs(usdjpy_pct) >= 1.0:
            points.append(
                f"USD/JPY の変動幅が拡大するかを確認する（今週{_pct_str(usdjpy_pct)}）"
            )

    return points[:5]
