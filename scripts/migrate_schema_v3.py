#!/usr/bin/env python3
"""
Marketcast Lab - スキーマ v3 移行ツール
========================================
events.json と event_reactions.json を新スキーマに移行する。

新スキーマの要点:
- tags を cause_tags（原因・手動）と effect_tags（結果・自動判定）に分離
- causal_chain（因果の向き）を任意フィールドとして用意（今は空でも可）
- context_snapshot（類似度検索用のマクロ環境ベクトル）を任意フィールドとして用意（今は空でも可）
- 結果タグ(effect_tags)は、各イベントの実反応データ(30日変化率)からルールで自動判定する

【結果タグ自動判定ルール（MVP・シンプル版）】
- WTI 30日変化率 >= +5%  → oil_spike（原油高）
- WTI 30日変化率 <= -5%  → oil_drop（原油安）
- S&P500 30日変化率 <= -5% → risk_off（リスクオフ）
- S&P500 30日変化率 >= +5% → risk_on（リスクオン）
- VIX 30日変化率 >= +20%  → volatility_up（変動性上昇）
- 米10年債利回り 30日変化率 > 0 → rate_up（金利上昇）
- 米10年債利回り 30日変化率 < 0 → rate_down（金利低下）

このスクリプトは「移行」用。本番の継続運用では calculate_event_reactions.py 側で
同じ判定を行うのが理想だが、フェーズ1では移行に集中する。
"""

import os
import json

HERE = os.path.dirname(__file__)
EVENTS_PATH = os.path.join(HERE, "..", "data", "events.json")
REACTIONS_PATH = os.path.join(HERE, "..", "data", "event_reactions.json")

# 旧tag → 原因タグ(cause)への対応。結果寄りの旧tagはここでは原因に含めない
OLD_TAG_TO_CAUSE = {
    "middle_east": "middle_east",        # 地域性は原因の文脈として保持
    "military_conflict": "war",
    "oil_supply": "supply_shock",
    "central_bank": None,                # 方向(緩和/引締)はイベント個別に手動付与するため自動移行しない
    "pandemic": "pandemic",
    "financial_shock": "bank_crisis",
    "liquidity_crisis": "liquidity_crisis",
    "inflation_shock": None,             # inflationは「結果」寄りなのでcauseからは除外
}

# 原因タグのラベル（日本語）
CAUSE_LABELS = {
    "war": "戦争・軍事衝突",
    "terror": "テロ",
    "supply_shock": "供給ショック",
    "demand_shock": "需要ショック",
    "monetary_tightening": "金融引き締め",
    "monetary_easing": "金融緩和",
    "emergency_cut": "緊急利下げ",
    "pandemic": "パンデミック",
    "bank_crisis": "銀行危機",
    "debt_crisis": "債務危機",
    "currency_crisis": "通貨危機",
    "natural_disaster": "自然災害",
    "middle_east": "中東地政学",
}

# 結果タグのラベル（日本語）
EFFECT_LABELS = {
    "oil_spike": "原油高",
    "oil_drop": "原油安",
    "inflation_up": "インフレ加速",
    "disinflation": "ディスインフレ",
    "usd_strong": "ドル高",
    "usd_weak": "ドル安",
    "risk_off": "リスクオフ",
    "risk_on": "リスクオン",
    "volatility_up": "変動性上昇",
    "rate_up": "金利上昇",
    "rate_down": "金利低下",
}


def auto_effect_tags(reactions):
    """イベントの反応データ(30日変化率)から結果タグを自動判定する。"""
    tags = []

    def chg(key):
        a = reactions.get(key)
        if not a or a.get("status") != "ok":
            return None
        return a.get("changes", {}).get("d30")

    wti = chg("wti")
    sp = chg("sp500")
    vix = chg("vix")
    rate = chg("ust10y")

    if wti is not None:
        if wti >= 5: tags.append("oil_spike")
        elif wti <= -5: tags.append("oil_drop")
    if sp is not None:
        if sp <= -5: tags.append("risk_off")
        elif sp >= 5: tags.append("risk_on")
    if vix is not None:
        if vix >= 20: tags.append("volatility_up")
    if rate is not None:
        if rate > 0: tags.append("rate_up")
        elif rate < 0: tags.append("rate_down")

    return tags


# イベントごとの原因タグ手動マッピング（旧データの意図を正しく原因レイヤーへ移す）
MANUAL_CAUSE = {
    "saudi_attack_2019":     ["middle_east", "supply_shock"],
    "us_iran_tension_2020":  ["middle_east", "war"],
    "russia_ukraine_2022":   ["war", "supply_shock"],
    "hamas_israel_2023":     ["middle_east", "war"],
    "iran_israel_strike_2024": ["middle_east", "war"],
    "fed_cut_start_2019":    ["monetary_easing"],
    "fed_emergency_cut_2020": ["emergency_cut", "pandemic"],
    "fed_hike_start_2022":   ["monetary_tightening"],
    "fed_hike_final_2023":   ["monetary_tightening"],
    "fed_cut_start_2024":    ["monetary_easing"],
}


def migrate():
    events = json.load(open(EVENTS_PATH, encoding="utf-8"))
    reactions = json.load(open(REACTIONS_PATH, encoding="utf-8"))

    react_map = {e["id"]: e.get("reactions", {}) for e in reactions["events"]}

    # events.json を移行
    events["schema_version"] = 3
    events["cause_labels"] = CAUSE_LABELS
    events["effect_labels"] = EFFECT_LABELS

    for ev in events["events"]:
        cause = MANUAL_CAUSE.get(ev["id"], [])
        effect = auto_effect_tags(react_map.get(ev["id"], {}))
        ev["cause_tags"] = cause
        ev["effect_tags"] = effect
        # 任意フィールドの器（将来用）
        ev.setdefault("causal_chain", [])
        ev.setdefault("context_snapshot", {})
        # 旧tagsは互換のため残すが、今後は使わない
        ev["legacy_tags"] = ev.pop("tags", [])

    json.dump(events, open(EVENTS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # event_reactions.json 側も同じ情報を反映
    reactions["cause_labels"] = CAUSE_LABELS
    reactions["effect_labels"] = EFFECT_LABELS
    emap = {e["id"]: e for e in events["events"]}
    for ev in reactions["events"]:
        src = emap.get(ev["id"])
        if src:
            ev["cause_tags"] = src["cause_tags"]
            ev["effect_tags"] = src["effect_tags"]
            ev["causal_chain"] = src.get("causal_chain", [])
            ev["context_snapshot"] = src.get("context_snapshot", {})
            ev["legacy_tags"] = src.get("legacy_tags", [])

    json.dump(reactions, open(REACTIONS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 確認出力
    print("移行完了 (schema v3)")
    for ev in events["events"]:
        print(f"  {ev['id']}: cause={ev['cause_tags']} effect={ev['effect_tags']}")


if __name__ == "__main__":
    migrate()
