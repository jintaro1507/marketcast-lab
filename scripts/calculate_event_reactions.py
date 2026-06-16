#!/usr/bin/env python3
"""
Marketcast Lab - 過去イベントの資産反応 計算スクリプト
=======================================================
events.json の各イベント日を起点に、FREDから各資産の価格を取得し、
1日後・7日後・30日後・90日後の変化率を計算する。
さらにタグごとに統計（上昇/下落回数・平均・中央値・最大・最小）を集計し、
event_reactions.json として出力する。

【設計方針】
- 未来予測ではなく、過去データの整理に徹する。
- 価格の生データは保存せず、変化率（%）のみを保存する（再配布制限への配慮）。
- イベント当日が休場でも、直近の取引日を基準日として採用する。
- 取得失敗した資産は status="error" として記録し、全体は壊さない。

実行:
    export FRED_API_KEY=あなたのキー
    python3 scripts/calculate_event_reactions.py
"""

import os
import json
import statistics
import datetime as dt
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

HERE = os.path.dirname(__file__)
EVENTS_PATH = os.path.join(HERE, "..", "data", "events.json")
OUT_PATH = os.path.join(HERE, "..", "data", "event_reactions.json")

# 対象資産（FRED系列ID）。restricted=Trueは再配布制限ありとして扱う
ASSETS = [
    {"key": "wti",    "label": "WTI原油",      "series": "DCOILWTICO", "asset": "oil",    "restricted": False},
    {"key": "gold",   "label": "金",            "series": "GOLDPMGBD228NLBM", "asset": "gold", "restricted": True},
    {"key": "sp500",  "label": "S&P500",        "series": "SP500",      "asset": "equity", "restricted": True},
    {"key": "ust10y", "label": "米10年債利回り","series": "DGS10",      "asset": "bond",   "restricted": False},
    {"key": "usdjpy", "label": "ドル円",        "series": "DEXJPUS",    "asset": "fx",     "restricted": False},
    {"key": "vix",    "label": "VIX",           "series": "VIXCLS",     "asset": "equity", "restricted": False},
]

HORIZONS = [
    ("d1", 1),
    ("d7", 7),
    ("d30", 30),
    ("d90", 90),
]


def fetch_series_range(series_id, start, end):
    """指定期間の観測値を {date: value} で返す。"""
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY が設定されていません")
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "observation_start": start.isoformat(),
        "observation_end": end.isoformat(),
        "sort_order": "asc",
    }
    url = f"{FRED_BASE}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "MarketcastLab/0.1"})
    with urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out = {}
    for obs in payload.get("observations", []):
        v = obs.get("value", ".")
        if v not in (".", "", None):
            try:
                out[obs["date"]] = float(v)
            except ValueError:
                pass
    return out


def value_on_or_before(series_map, target_date, max_lookback=7):
    """target_date 当日、なければ直前の取引日の値を返す（最大max_lookback日遡る）。"""
    for back in range(0, max_lookback + 1):
        d = (target_date - dt.timedelta(days=back)).isoformat()
        if d in series_map:
            return series_map[d], d
    return None, None


def compute_reactions_for_event(event):
    """1イベントについて、各資産の各期間の変化率を計算。"""
    base_date = dt.date.fromisoformat(event["date"])
    # 余裕をもって前後の期間を取得（基準日が休場の場合の遡り＋90日後＋バッファ）
    start = base_date - dt.timedelta(days=10)
    end = base_date + dt.timedelta(days=110)

    assets_out = {}
    for a in ASSETS:
        entry = {"label": a["label"], "asset": a["asset"], "restricted": a["restricted"], "status": "ok", "changes": {}}
        try:
            series_map = fetch_series_range(a["series"], start, end)
            base_val, used_base_date = value_on_or_before(series_map, base_date)
            if base_val is None or base_val == 0:
                entry["status"] = "no_data"
            else:
                entry["base_date"] = used_base_date
                for name, days in HORIZONS:
                    fut_val, _ = value_on_or_before(series_map, base_date + dt.timedelta(days=days))
                    if fut_val is None:
                        entry["changes"][name] = None
                    else:
                        # 金利(bond)は「利回りの変化幅(pt)」ではなく相対変化率(%)で統一
                        entry["changes"][name] = round((fut_val - base_val) / abs(base_val) * 100.0, 1)
        except (URLError, HTTPError, RuntimeError) as e:
            entry["status"] = "error"
            entry["error"] = str(e)
        assets_out[a["key"]] = entry
    return assets_out


def summarize_by_tag(events_with_reactions, horizon="d30"):
    """
    タグごとに、指定期間(既定30日後)の各資産の変化率を集計する。
    上昇/下落回数・平均・中央値・最大・最小を返す。
    """
    tag_stats = {}
    for ev in events_with_reactions:
        for tag in ev["tags"]:
            tag_stats.setdefault(tag, {})
            for akey, adata in ev["reactions"].items():
                if adata["status"] != "ok":
                    continue
                val = adata["changes"].get(horizon)
                if val is None:
                    continue
                tag_stats[tag].setdefault(akey, {"label": adata["label"], "asset": adata["asset"], "values": []})
                tag_stats[tag][akey]["values"].append(val)

    # 集計
    summary = {}
    for tag, assets in tag_stats.items():
        summary[tag] = {}
        for akey, info in assets.items():
            vals = info["values"]
            if not vals:
                continue
            ups = sum(1 for v in vals if v > 0)
            downs = sum(1 for v in vals if v < 0)
            summary[tag][akey] = {
                "label": info["label"],
                "asset": info["asset"],
                "count": len(vals),
                "up": ups,
                "down": downs,
                "avg": round(statistics.mean(vals), 1),
                "median": round(statistics.median(vals), 1),
                "max": round(max(vals), 1),
                "min": round(min(vals), 1),
            }
    return summary


def build():
    with open(EVENTS_PATH, encoding="utf-8") as f:
        events_master = json.load(f)

    events_with_reactions = []
    for ev in events_master["events"]:
        reactions = compute_reactions_for_event(ev)
        events_with_reactions.append({
            "id": ev["id"],
            "name": ev["name"],
            "date": ev["date"],
            "category": ev.get("category"),
            "tags": ev["tags"],
            "description": ev["description"],
            "similarity_reason": ev.get("similarity_reason", ""),
            "propagation": ev.get("propagation", []),
            "sources": ev.get("sources", []),
            "reactions": reactions,
        })

    summary = summarize_by_tag(events_with_reactions, horizon="d30")

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tag_labels": events_master["tag_labels"],
        "category_labels": events_master.get("category_labels", {}),
        "horizons": [h[0] for h in HORIZONS],
        "summary_horizon": "d30",
        "events": events_with_reactions,
        "tag_summary": summary,
        "disclaimer": "本データは過去の同種イベント発生後の市場反応を記録・整理したものであり、将来の値動きを示すものでも、売買を推奨するものでもありません。",
    }


if __name__ == "__main__":
    payload = build()
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {OUT_PATH}")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:1200])
