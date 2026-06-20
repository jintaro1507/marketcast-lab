#!/usr/bin/env python3
"""
quality_gate.py — デプロイ前品質ゲート（KI-003）

生成JSONの重大な異常を検出してデプロイを停止する。
exit 0: 正常（Warningのみ含む場合も含む）
exit 1: Fail検出 → upload-pages-artifact と deploy-pages をスキップさせる

変更対象外: fetch_and_build.py / calculate_event_reactions.py
"""

import json, os, sys
from pathlib import Path
import datetime as dt

HERE = Path(__file__).parent.parent  # リポジトリルート

# 検査対象ファイル
ER_PATH  = HERE / "data" / "event_reactions.json"
EV_PATH  = HERE / "data" / "events.json"
MKT_PATH = HERE / "data" / "market.json"
CC_PATH  = HERE / "data" / "current_context_public.json"

# 資産別error率閾値
ASSET_ERROR_FAIL_RATE    = 0.50  # 任意の資産で全イベントの50%以上error → Fail
ASSET_ERROR_WARN_MIN     = 1     # 1件以上error → Warning

# market.json 必須系列
MARKET_REQUIRED = ["DCOILWTICO", "VIXCLS", "DGS10"]
MARKET_FAIL_MIN  = 2  # 2件以上errorかつvalue欠落 → Fail（1件はWarning）

# 機密情報検査パターン（実値は環境変数から取得、ログには出さない）
SECRET_PATTERNS = ["api_key=", "FRED_API_KEY"]

ASSETS = ["wti", "gold", "sp500", "ust10y", "usdjpy", "vix"]

# current_context_public.json 検査用定数
CC_REQUIRED_TOP = [
    "generated_at", "indicators", "market_state_tags",
    "rate_display", "data_completeness", "free_top_match",
    "disclaimer", "data_note",
]
CC_INDICATOR_KEYS = ["vix", "oil", "ff", "ust10y"]
CC_VALID_STATUS   = {"ok", "stale", "unavailable"}
CC_VALID_VIX      = {"calm", "elev", "stress", "panic"}
CC_VALID_OIL      = {"lo", "mid", "hi"}
CC_VALID_RATE     = {"low", "mid", "high"}
CC_VALID_RATE_LABEL = {"低", "中", "高", None}  # rate タグなし時は None
CC_VALID_CURVE    = {"normal", "flat", "inverted", None}
# CSN Step1 の必須 8 分類（index.html から確認済み）
CC_CSN_CAUSE_TAGS = [
    "supply_shock", "bank_crisis", "war", "middle_east",
    "monetary_tightening", "monetary_easing", "emergency_cut", "pandemic",
]
# free_top_match に含まれてはいけない有料加工結果キー
CC_PAID_KEYS = {
    "candidates", "reaction_summary", "matched_tags",
    "mismatched_tags", "unavailable_tags", "reliability",
}
# generated_at の未来許容幅（Actions の時計ズレ等を考慮して5分）
CC_FUTURE_TOLERANCE_MINUTES = 5


def load_json(path):
    if not path.exists():
        return None, f"ファイルが存在しない: {path.name}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as e:
        return None, f"JSON構文エラー ({path.name}): {e}"


def check_event_reactions(er, ev_count):
    """event_reactions.json の全検査。(fails, warnings) のリストを返す。"""
    fails, warns = [], []

    # 必須トップキー
    for key in ["events", "generated_at", "cause_summary"]:
        if key not in er:
            fails.append(f"必須キー欠落: {key}")

    events = er.get("events", [])

    # イベント件数一致
    er_count = len(events)
    if er_count != ev_count:
        fails.append(
            f"イベント件数不一致: events.json={ev_count}件 / "
            f"event_reactions.json={er_count}件"
        )

    if not events:
        return fails, warns  # 以降の検査不能

    # 資産別 error 率（全イベント横断）
    total = len(events)
    for asset in ASSETS:
        error_n = sum(
            1 for ev in events
            if ev.get("reactions", {}).get(asset, {}).get("status") == "error"
        )
        if error_n == 0:
            continue
        rate = error_n / total
        if rate >= ASSET_ERROR_FAIL_RATE:
            fails.append(
                f"資産 {asset}: error率 {error_n}/{total} "
                f"({rate:.0%}) が閾値({ASSET_ERROR_FAIL_RATE:.0%})超"
            )
        else:
            warns.append(
                f"資産 {asset}: {error_n}件のerror "
                f"({rate:.0%}) ― 部分的なAPI障害の可能性"
            )

    # 1イベント内の error 集中チェック（3資産以上 error → Fail）
    # no_data は含めない（系列提供範囲外は正常な欠損）
    EVENT_ERROR_FAIL_N = 3
    for ev in events:
        error_n = sum(
            1 for a in ev.get("reactions", {}).values()
            if a.get("status") == "error"
        )
        if error_n >= EVENT_ERROR_FAIL_N:
            fails.append(
                f"イベント {ev.get('id','?')}: {error_n}資産が error "
                f"（閾値={EVENT_ERROR_FAIL_N}資産）― 429等のAPI障害の可能性"
            )

    # 機密情報混入
    raw_text = json.dumps(er)
    fred_key = os.environ.get("FRED_API_KEY", "")
    patterns_named = [(p, p) for p in SECRET_PATTERNS]
    if fred_key:
        patterns_named.append((fred_key, "FRED_API_KEYの実値"))
    for pat, label in patterns_named:
        if pat and pat in raw_text:
            fails.append(f"機密情報検出: {label}")

    return fails, warns


def check_market(mkt):
    """market.json の検査。(fails, warnings) を返す。"""
    fails, warns = [], []

    # generated_at 必須
    if not mkt.get("generated_at"):
        fails.append("market.json: generated_at が欠落")

    # series が配列であること
    series_list = mkt.get("series")
    if not isinstance(series_list, list):
        fails.append("market.json: series が配列でない")
        return fails, warns

    # id をキーとして系列を検索（fetch_and_build.py が id を出力するようになったため）
    series_by_id = {s.get("id"): s for s in series_list if isinstance(s, dict)}

    for sid in MARKET_REQUIRED:
        s = series_by_id.get(sid)
        if s is None:
            fails.append(f"market.json: 必須系列 {sid} が見つからない")
            continue
        if s.get("status") == "error" and s.get("latest_value") is None and s.get("pct_change") is None:
            warns.append(f"market.json: {sid} が error かつ value 欠落")

    # 必須3系列のうち2件以上が error かつ value 欠落 → Fail
    bad = [sid for sid in MARKET_REQUIRED
           if series_by_id.get(sid, {}).get("status") == "error"
           and series_by_id.get(sid, {}).get("latest_value") is None
           and series_by_id.get(sid, {}).get("pct_change") is None]
    if len(bad) >= 2:
        # warns に追加済みのものを fails に昇格
        warns = [w for w in warns if not any(b in w for b in bad)]
        fails.append(
            f"market.json: 必須系列 {len(bad)}件が error かつ value 欠落 "
            f"({', '.join(bad)})"
        )

    return fails, warns


def check_current_context(cc, er_event_ids):
    """current_context_public.json の全検査。(fails, warnings) のリストを返す。
    er_event_ids: event_reactions.json 内の全 event_id のセット（event_id 整合確認用）。
    """
    fails, warns = [], []

    # ── 必須トップキー ──
    for key in CC_REQUIRED_TOP:
        if key not in cc:
            fails.append(f"current_context: 必須キー欠落: {key}")

    if fails:
        return fails, warns  # キー欠落があると以降の検査が意味をなさないため早期リターン

    # ── generated_at ──
    gen_str = cc.get("generated_at", "")
    try:
        gen_dt = dt.datetime.fromisoformat(gen_str)
        # タイムゾーン情報があれば UTC に変換して比較、なければ naive のまま
        now_utc = dt.datetime.now(dt.timezone.utc)
        if gen_dt.tzinfo:
            gen_utc = gen_dt.astimezone(dt.timezone.utc)
        else:
            gen_utc = gen_dt
            now_utc = dt.datetime.utcnow()
        future_limit = now_utc + dt.timedelta(minutes=CC_FUTURE_TOLERANCE_MINUTES)
        if gen_utc > future_limit:
            fails.append(
                f"current_context: generated_at が現在時刻より大幅に未来: {gen_str}"
            )
    except ValueError:
        fails.append(f"current_context: generated_at が ISO 8601 として解析不能: {gen_str!r}")

    # ── indicators ──
    indicators = cc.get("indicators", {})
    if not isinstance(indicators, dict):
        fails.append("current_context: indicators が dict でない")
    else:
        for key in CC_INDICATOR_KEYS:
            ind = indicators.get(key)
            if ind is None:
                fails.append(f"current_context: indicators.{key} が欠落")
                continue
            for subkey in ("value", "as_of", "status"):
                if subkey not in ind:
                    fails.append(f"current_context: indicators.{key}.{subkey} が欠落")
            status = ind.get("status")
            if status not in CC_VALID_STATUS:
                fails.append(
                    f"current_context: indicators.{key}.status が不正値: {status!r} "
                    f"(許可: {sorted(CC_VALID_STATUS)})"
                )
            elif status == "stale":
                warns.append(f"current_context: indicators.{key} が stale")
            elif status == "unavailable":
                warns.append(f"current_context: indicators.{key} が unavailable")
                # unavailable なのに value/as_of が非 null → Warning
                if ind.get("value") is not None or ind.get("as_of") is not None:
                    warns.append(
                        f"current_context: indicators.{key} が unavailable だが "
                        f"value/as_of が非 null"
                    )
            # ok/stale のとき value が数値・as_of が ISO 日付として解析可能か検査
            if status in ("ok", "stale"):
                if not isinstance(ind.get("value"), (int, float)):
                    fails.append(
                        f"current_context: indicators.{key}.value が数値でない "
                        f"(status={status!r})"
                    )
                try:
                    dt.date.fromisoformat(ind.get("as_of", ""))
                except (ValueError, TypeError):
                    fails.append(
                        f"current_context: indicators.{key}.as_of が ISO 日付として "
                        f"解析不能 (status={status!r})"
                    )

    # ── data_completeness ──
    dc = cc.get("data_completeness")
    if not isinstance(dc, int) or dc < 0 or dc > 3:
        fails.append(
            f"current_context: data_completeness が範囲外: {dc!r} (0〜3 の整数)"
        )
    elif dc == 0:
        fails.append("current_context: data_completeness=0 (全指標が取得不能)")
    elif dc in (1, 2):
        warns.append(f"current_context: data_completeness={dc} (比較可能軸が少ない)")

    # ── market_state_tags ──
    mst = cc.get("market_state_tags", {})
    if not isinstance(mst, dict):
        fails.append("current_context: market_state_tags が dict でない")
    else:
        for axis, valid_set, label in [
            ("vix",  CC_VALID_VIX,  "calm/elev/stress/panic"),
            ("oil",  CC_VALID_OIL,  "lo/mid/hi"),
            ("rate", CC_VALID_RATE, "low/mid/high"),
        ]:
            val = mst.get(axis)
            if val is not None and val not in valid_set:
                fails.append(
                    f"current_context: market_state_tags.{axis} が不正値: "
                    f"{val!r} (許可: {label})"
                )

    # ── rate_display ──
    rd = cc.get("rate_display", {})
    if not isinstance(rd, dict):
        fails.append("current_context: rate_display が dict でない")
    else:
        rel = rd.get("rate_env_label")
        if rel not in CC_VALID_RATE_LABEL:
            fails.append(
                f"current_context: rate_display.rate_env_label が不正値: "
                f"{rel!r} (許可: 低/中/高)"
            )
        curve = rd.get("curve_note")
        if curve not in CC_VALID_CURVE:
            fails.append(
                f"current_context: rate_display.curve_note が不正値: "
                f"{curve!r} (許可: normal/flat/inverted/null)"
            )

    # ── free_top_match ──
    ftm = cc.get("free_top_match", {})
    if not isinstance(ftm, dict):
        fails.append("current_context: free_top_match が dict でない")
    else:
        # 必須 8 分類の存在確認
        for tag in CC_CSN_CAUSE_TAGS:
            if tag not in ftm:
                fails.append(f"current_context: free_top_match に必須 cause_tag 欠落: {tag}")

        # event_id 整合確認・overlap_label 確認・有料加工結果混入確認
        for tag, entry in ftm.items():
            if not isinstance(entry, dict):
                fails.append(
                    f"current_context: free_top_match.{tag} が dict でない "
                    f"(型: {type(entry).__name__!r})"
                )
                continue
            eid = entry.get("event_id")
            if eid is not None and eid not in er_event_ids:
                fails.append(
                    f"current_context: free_top_match.{tag}.event_id={eid!r} が "
                    f"event_reactions.json に存在しない"
                )
            if eid is None:
                warns.append(
                    f"current_context: free_top_match.{tag} の event_id が null "
                    f"(overlap_label={entry.get('overlap_label')!r})"
                )
            # overlap_label の許可値チェック
            VALID_OVERLAP = {"高い重なり", "中程度の重なり", "部分的な重なり", "明確な類似局面なし"}
            ol = entry.get("overlap_label")
            if ol not in VALID_OVERLAP:
                fails.append(
                    f"current_context: free_top_match.{tag}.overlap_label が不正値: "
                    f"{ol!r}"
                )

            # event_id と overlap_label の整合性
            if eid is not None:
                # event_id あり → 高い重なり or 中程度の重なり のみ
                if ol not in ("高い重なり", "中程度の重なり"):
                    fails.append(
                        f"current_context: free_top_match.{tag}: event_id あり だが "
                        f"overlap_label={ol!r} (高い重なり/中程度の重なり のみ許可)"
                    )
                # event_id あり → name/date/matched_summary/cta が必要
                for req in ("name", "date", "matched_summary", "cta"):
                    if not entry.get(req):
                        fails.append(
                            f"current_context: free_top_match.{tag}: "
                            f"event_id あり だが {req!r} が欠落"
                        )
            else:
                # event_id なし → 部分的な重なり or 明確な類似局面なし のみ
                if ol not in ("部分的な重なり", "明確な類似局面なし"):
                    fails.append(
                        f"current_context: free_top_match.{tag}: event_id=null だが "
                        f"overlap_label={ol!r} (部分的な重なり/明確な類似局面なし のみ許可)"
                    )
                # event_id なし → overlap_label と matched_summary が必要
                for req in ("overlap_label", "matched_summary"):
                    if not entry.get(req):
                        fails.append(
                            f"current_context: free_top_match.{tag}: "
                            f"event_id=null だが {req!r} が欠落"
                        )

            # 有料加工結果キーの混入チェック
            for paid_key in CC_PAID_KEYS:
                if paid_key in entry:
                    fails.append(
                        f"current_context: free_top_match.{tag} に有料加工結果キー混入: "
                        f"{paid_key!r}"
                    )

    return fails, warns


def main():
    all_fails, all_warns = [], []

    # event_reactions.json
    er, err = load_json(ER_PATH)
    if err:
        print(f"::error::{err}")
        sys.exit(1)

    # events.json（件数比較用）
    ev, err2 = load_json(EV_PATH)
    if err2:
        print(f"::error::{err2}")
        sys.exit(1)
    ev_count = len(ev.get("events", []))

    # market.json
    mkt, err3 = load_json(MKT_PATH)
    if err3:
        all_fails.append(f"market.json 読み込み失敗: {err3}")
        mkt = None

    # current_context_public.json
    cc, err4 = load_json(CC_PATH)
    if err4:
        all_fails.append(f"current_context_public.json 読み込み失敗: {err4}")
        cc = None

    # 全検査を実行してから結果をまとめる
    f1, w1 = check_event_reactions(er, ev_count)
    all_fails.extend(f1)
    all_warns.extend(w1)

    if mkt:
        f2, w2 = check_market(mkt)
        all_fails.extend(f2)
        all_warns.extend(w2)

    if cc:
        er_event_ids = {e["id"] for e in er.get("events", [])}
        f3, w3 = check_current_context(cc, er_event_ids)
        all_fails.extend(f3)
        all_warns.extend(w3)

    # まとめてログ出力
    ev_n = len(er.get("events", []))
    print(f"[quality_gate] event_reactions: {ev_n}件 / events.json: {ev_count}件")

    for w in all_warns:
        print(f"::warning::{w}")

    if all_fails:
        for f in all_fails:
            print(f"::error::{f}")
        print(f"[quality_gate] FAIL ({len(all_fails)}件) — デプロイを停止します")
        sys.exit(1)

    if all_warns:
        print(f"[quality_gate] PASS with {len(all_warns)} warning(s) — デプロイ継続")
    else:
        print("[quality_gate] PASS — デプロイ継続")
    sys.exit(0)


if __name__ == "__main__":
    main()
