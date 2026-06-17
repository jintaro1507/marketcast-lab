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
import time
import statistics
import datetime as dt
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

HERE = os.path.dirname(__file__)
EVENTS_PATH = os.path.join(HERE, "..", "data", "events.json")
GROUP_META_PATH = os.path.join(HERE, "..", "data", "group_metadata.json")
OUT_PATH = os.path.join(HERE, "..", "data", "event_reactions.json")

# 対象資産。source=fred は FRED API、source=stooq は Stooq から取得。
# restricted=Trueは再配布制限があるため生値を出さず変化率/派生値のみ表示。
# 金は LBMA系列(FRED)が更新停止のため、金ETF GLD(Stooq)を金価格の代理指標として使用する。
ASSETS = [
    {"key": "wti",    "label": "WTI原油",      "source": "fred",  "series": "DCOILWTICO", "asset": "oil",    "restricted": False},
    {"key": "gold",   "label": "金（GLD）",     "source": "stooq", "series": "gld.us",     "yahoo_symbol": "GLD", "asset": "gold",   "restricted": True},
    {"key": "sp500",  "label": "S&P500",        "source": "fred",  "series": "SP500",      "asset": "equity", "restricted": True},
    {"key": "ust10y", "label": "米10年債利回り","source": "fred",  "series": "DGS10",      "asset": "bond",   "restricted": False},
    {"key": "usdjpy", "label": "ドル円",        "source": "fred",  "series": "DEXJPUS",    "asset": "fx",     "restricted": False},
    {"key": "vix",    "label": "VIX",           "source": "fred",  "series": "VIXCLS",     "asset": "equity", "restricted": False},
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


def fetch_stooq_range(symbol, start, end):
    """Stooqから日次終値を {date: value} で返す。キー不要・標準ライブラリのみ。
    ブラウザ風ヘッダとリトライ(最大3回)を入れ、データセンターIPでの取得失敗に備える。
    例: https://stooq.com/q/d/l/?s=gld.us&d1=20220101&d2=20220401&i=d"""
    params = {
        "s": symbol,
        "d1": start.strftime("%Y%m%d"),
        "d2": end.strftime("%Y%m%d"),
        "i": "d",
    }
    url = f"https://stooq.com/q/d/l/?{urlencode(params)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/csv,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_err = None
    for attempt in range(1, 4):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8")
            lines = text.strip().splitlines()
            if not lines or not lines[0].lower().startswith("date"):
                first = text[:80]
                # HTMLが返ってきた = ブロックされている。リトライしても無駄なので即終了
                if "<!doctype" in text.lower() or "<html" in text.lower():
                    raise RuntimeError(f"Stooqにブロックされました（HTML応答）: {first!r}")
                raise RuntimeError(f"有効なCSVヘッダなし（応答先頭: {first!r}）")
            header = lines[0].split(",")
            try:
                close_idx = [h.lower() for h in header].index("close")
            except ValueError:
                close_idx = 4
            out = {}
            for line in lines[1:]:
                cols = line.split(",")
                if len(cols) <= close_idx:
                    continue
                try:
                    out[cols[0]] = float(cols[close_idx])
                except ValueError:
                    pass
            if not out:
                raise RuntimeError("CSVは取得したがデータ行が空")
            return out
        except RuntimeError as e:
            # ブロック（HTML応答）はリトライしない
            if "ブロック" in str(e):
                print(f"  [Stooq] {symbol} ブロック確認、リトライ中止")
                raise
            last_err = e
            print(f"  [Stooq] {symbol} 取得失敗 (試行{attempt}/3): {e}")
            time.sleep(2 * attempt)
        except (URLError, HTTPError) as e:
            # 接続エラーはリトライする価値あり
            last_err = e
            print(f"  [Stooq] {symbol} 接続エラー (試行{attempt}/3): {e}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"Stooq取得に3回失敗: {symbol} / 最終エラー: {last_err}")


def fetch_yahoo_range(symbol, start, end):
    """Yahoo Financeから日次終値を {date: value} で返す（Stooq失敗時のフォールバック）。
    yfinanceがあれば使い、無ければYahooのchart APIを直接叩く。"""
    # まず yfinance を試す（0.2系以降のAPI対応）
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start.isoformat(),
                            end=(end + dt.timedelta(days=1)).isoformat(),
                            interval="1d", auto_adjust=True)
        out = {}
        if df is not None and len(df) > 0:
            for idx, row in df.iterrows():
                try:
                    # 新APIはindexがTimestamp型（tzあり）
                    if hasattr(idx, 'date'):
                        d = idx.date().isoformat()
                    else:
                        d = str(idx)[:10]
                    out[d] = float(row["Close"])
                except (ValueError, TypeError, KeyError):
                    pass
        if out:
            print(f"  [Yahoo/yfinance] {symbol} 取得成功: {len(out)}件")
            return out
        raise RuntimeError("yfinanceの応答が空")
    except ImportError:
        pass  # yfinance未導入ならchart APIへ
    except Exception as e:
        print(f"  [Yahoo/yfinance] {symbol} 取得失敗: {e} → chart APIを試行")

    # フォールバックのフォールバック: Yahoo chart APIを直接叩く（v8は廃止、v8/financeに修正）
    period1 = int(dt.datetime(start.year, start.month, start.day,
                              tzinfo=dt.timezone.utc).timestamp())
    period2 = int(dt.datetime(end.year, end.month, end.day,
                              tzinfo=dt.timezone.utc).timestamp()) + 86400
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={period1}&period2={period2}&interval=1d&events=history")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0].get("close", [])
    out = {}
    for ts, c in zip(timestamps, closes):
        if c is None:
            continue
        d = dt.datetime.utcfromtimestamp(ts).date().isoformat()
        out[d] = float(c)
    if not out:
        raise RuntimeError("Yahoo chart APIの応答が空")
    print(f"  [Yahoo/chart API] {symbol} 取得成功: {len(out)}件")
    return out


def fetch_range(asset, start, end):
    """資産のsourceに応じて取得元を振り分ける。返却形式は {date: value} で統一。
    金(stooq指定)はStooq→Yahooのフォールバックを行い、(値, 実際の取得元)を返す。
    その他はFREDから取得し、取得元'fred'を返す。"""
    src = asset.get("source", "fred")
    if src == "stooq":
        try:
            return fetch_stooq_range(asset["series"], start, end), "stooq"
        except (URLError, HTTPError, RuntimeError) as e:
            print(f"  [フォールバック] {asset['key']}: Stooq失敗のためYahooへ切替 ({e})")
            ysym = asset.get("yahoo_symbol", asset["series"].replace(".us", "").upper())
            return fetch_yahoo_range(ysym, start, end), "yahoo"
    return fetch_series_range(asset["series"], start, end), "fred"


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
        is_yield = (a["asset"] == "bond")
        if is_yield:
            entry["changes_pt"] = {}  # 利回りの絶対変化幅(pt)
        try:
            series_map, used_source = fetch_range(a, start, end)
            entry["source"] = used_source
            base_val, used_base_date = value_on_or_before(series_map, base_date)
            if base_val is None or base_val == 0:
                entry["status"] = "no_data"
            else:
                entry["base_date"] = used_base_date
                for name, days in HORIZONS:
                    fut_val, _ = value_on_or_before(series_map, base_date + dt.timedelta(days=days))
                    if fut_val is None:
                        entry["changes"][name] = None
                        if is_yield:
                            entry["changes_pt"][name] = None
                    else:
                        # 相対変化率(%)はすべての資産で保存（表示の統一用）
                        entry["changes"][name] = round((fut_val - base_val) / abs(base_val) * 100.0, 1)
                        # 利回りは絶対変化幅(pt)も保存（タグ判定はこちらを使う）
                        if is_yield:
                            entry["changes_pt"][name] = round(fut_val - base_val, 2)
        except (URLError, HTTPError, RuntimeError) as e:
            entry["status"] = "error"
            entry["error"] = str(e)
        assets_out[a["key"]] = entry
    return assets_out


def detect_effect_tags(reactions, effect_rules, horizon="d30"):
    """1日後・7日後・30日後の3期間いずれかで閾値を超えた場合にタグを付与する。
    90日後はノイズ（別イベントの混入）が大きいためタグ判定から除外。90日のデータは表示用に保持。
    返値: (effect_tags リスト, effect_tag_details 辞書)
    effect_tag_details = {"oil_up": ["d1","d7"], "gold_up": ["d30"]} のように期間情報を保持。
    引数 horizon は後方互換のために残すが、3期間チェックに変更したため使用しない。
    risk_on/risk_off などの解釈タグは使わない（観察事実タグのみ）。"""
    TAG_HORIZONS = ["d1", "d7", "d30"]  # 90日はタグ判定対象外
    tag_periods = {}

    for tag, rule in effect_rules.items():
        a = reactions.get(rule["asset"])
        if not a or a.get("status") != "ok":
            continue
        mode = rule.get("mode", "pct")
        op, th = rule["op"], rule["threshold"]

        for h in TAG_HORIZONS:
            if mode == "abs_pt":
                v = a.get("changes_pt", {}).get(h)
            else:
                v = a.get("changes", {}).get(h)
            if v is None:
                continue
            hit = (op == ">=" and v >= th) or (op == "<=" and v <= th)
            if hit:
                tag_periods.setdefault(tag, []).append(h)

    effect_tags = sorted(tag_periods.keys())
    effect_tag_details = {tag: periods for tag, periods in tag_periods.items()}
    return effect_tags, effect_tag_details


def confidence_for(n, levels):
    """件数nから信頼性レベル定義を返す。"""
    for lv in levels:
        if n <= lv["max_n"]:
            return {"level": lv["level"], "label": lv["label"], "note": lv.get("note", "")}
    return {"level": "high", "label": "信頼性：高", "note": ""}


def summarize_by_cause(events_with_reactions, horizon="d30", conf_levels=None):
    """
    原因タグ(cause_tags)ごとに、指定期間(既定30日後)の各資産の変化率を集計する。
    上昇/下落回数・平均・中央値・最大・最小に加え、件数nと信頼性レベルを返す。
    """
    tag_stats = {}
    for ev in events_with_reactions:
        for tag in ev["cause_tags"]:
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
            n = len(vals)
            entry = {
                "label": info["label"],
                "asset": info["asset"],
                "count": n,
                "up": ups,
                "down": downs,
                "avg": round(statistics.mean(vals), 1),
                "median": round(statistics.median(vals), 1),
                "max": round(max(vals), 1),
                "min": round(min(vals), 1),
            }
            if conf_levels:
                entry["confidence"] = confidence_for(n, conf_levels)
            summary[tag][akey] = entry
    return summary


def build():
    with open(EVENTS_PATH, encoding="utf-8") as f:
        events_master = json.load(f)

    effect_rules = events_master.get("effect_rules", {})
    conf_levels = events_master.get("confidence_levels", [])

    events_with_reactions = []
    for ev in events_master["events"]:
        reactions = compute_reactions_for_event(ev)
        # 結果タグは全期間(1日/7日/30日/90日)のいずれかで閾値超えなら付与（全期間チェック方式）
        effect_tags, effect_tag_details = detect_effect_tags(reactions, effect_rules)
        events_with_reactions.append({
            "id": ev["id"],
            "name": ev["name"],
            "date": ev["date"],
            "category": ev.get("category"),
            "cause_tags": ev.get("cause_tags", []),
            "effect_tags": effect_tags,
            "effect_tag_details": effect_tag_details,
            "causal_chain": ev.get("causal_chain", []),
            "context_snapshot": {
                "vix_level": ev.get("context_snapshot", {}).get("vix_level"),
                "oil_price_wti": ev.get("context_snapshot", {}).get("oil_price_wti"),
                "cpi_yoy": ev.get("context_snapshot", {}).get("cpi_yoy"),
                "fed_funds_rate": ev.get("context_snapshot", {}).get("fed_funds_rate"),
                "ust10y_yield": ev.get("context_snapshot", {}).get("ust10y_yield"),
            },
            "description": ev["description"],
            "similarity_reason": ev.get("similarity_reason", ""),
            "why_reaction": ev.get("why_reaction", ""),
            "key_insight": ev.get("key_insight", ""),
            "contrast": [
                dict(c, type=c.get("type", "structural_contrast"))
                for c in ev.get("contrast", [])
            ],
            "propagation": ev.get("propagation", []),
            "sources": ev.get("sources", []),
            "reactions": reactions,
        })

    summary = summarize_by_cause(events_with_reactions, horizon="d30", conf_levels=conf_levels)

    # グループメタ情報を合流（存在すれば）。中身が空(summary空)のグループも含めて渡し、表示判定はUI側で行う。
    group_meta = {}
    if os.path.exists(GROUP_META_PATH):
        with open(GROUP_META_PATH, encoding="utf-8") as f:
            group_meta = json.load(f).get("groups", {})

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cause_tag_labels": events_master.get("cause_tag_labels", {}),
        "cause_tag_hierarchy": events_master.get("cause_tag_hierarchy", {}),
        "effect_tag_labels": events_master.get("effect_tag_labels", {}),
        "category_labels": events_master.get("category_labels", {}),
        "confidence_levels": conf_levels,
        "horizons": [h[0] for h in HORIZONS],
        "summary_horizon": "d30",
        "events": events_with_reactions,
        "event_names": {e["id"]: e["name"] for e in events_with_reactions},
        "cause_summary": summary,
        "group_metadata": group_meta,
        "disclaimer": "本データは過去の同種イベント発生後の市場反応を記録・整理したものであり、将来の値動きを示すものでも、売買を推奨するものでもありません。",
    }


if __name__ == "__main__":
    payload = build()
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {OUT_PATH}")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:1200])
