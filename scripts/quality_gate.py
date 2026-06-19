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

HERE = Path(__file__).parent.parent  # リポジトリルート

# 検査対象ファイル
ER_PATH  = HERE / "data" / "event_reactions.json"
EV_PATH  = HERE / "data" / "events.json"
MKT_PATH = HERE / "data" / "market.json"

# 資産別error率閾値
ASSET_ERROR_FAIL_RATE    = 0.50  # 任意の資産で全イベントの50%以上error → Fail
ASSET_ERROR_WARN_MIN     = 1     # 1件以上error → Warning

# market.json 必須系列
MARKET_REQUIRED = ["DCOILWTICO", "VIXCLS", "DGS10"]
MARKET_FAIL_MIN  = 2  # 2件以上errorかつvalue欠落 → Fail（1件はWarning）

# 機密情報検査パターン（実値は環境変数から取得、ログには出さない）
SECRET_PATTERNS = ["api_key=", "FRED_API_KEY"]

ASSETS = ["wti", "gold", "sp500", "ust10y", "usdjpy", "vix"]


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

    # 資産別 error 率
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
    """market.json の必須系列検査。(fails, warnings) を返す。"""
    fails, warns = [], []
    series_list = mkt.get("series", [])

    # series が list か dict かを吸収（label または id でマッチ）
    def find_series(sid):
        for s in series_list:
            if isinstance(s, dict):
                if s.get("id") == sid or s.get("label","").startswith(sid[:4]):
                    return s
        return None

    # series が dict のキー形式の場合も対応
    if isinstance(series_list, dict):
        bad_n = sum(
            1 for sid in MARKET_REQUIRED
            if series_list.get(sid, {}).get("status") == "error"
            and series_list.get(sid, {}).get("latest_value") is None
            and series_list.get(sid, {}).get("pct_change") is None
        )
    else:
        bad = []
        for s in series_list:
            sid = s.get("id", s.get("label",""))
            is_required = any(req in sid for req in MARKET_REQUIRED)
            if is_required and s.get("status") == "error":
                if s.get("latest_value") is None and s.get("pct_change") is None:
                    bad.append(sid)
        bad_n = len(bad)

    if bad_n >= MARKET_FAIL_MIN:
        fails.append(
            f"market.json: 必須系列 {bad_n}件がerrorかつvalue欠落 "
            f"(閾値={MARKET_FAIL_MIN}件)"
        )
    elif bad_n == 1:
        warns.append(f"market.json: 必須系列 1件がerrorかつvalue欠落")

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

    # 全検査を実行してから結果をまとめる
    f1, w1 = check_event_reactions(er, ev_count)
    all_fails.extend(f1)
    all_warns.extend(w1)

    if mkt:
        f2, w2 = check_market(mkt)
        all_fails.extend(f2)
        all_warns.extend(w2)

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
