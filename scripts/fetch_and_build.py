#!/usr/bin/env python3
"""
Marketcast Lab - 市場現況データ取得＆加工スクリプト
====================================================
公開情報（FRED経済データ + Stooq/Yahoo Finance）を取得し、
各資産クラスの「30日変化率・方向」を計算して market.json を出力する。

【重要な設計方針】
- 価格の「生データ」をそのまま再配布せず、変化率(%)など加工した値を出力する。
  FREDの一部系列(S&P500など)は再配布制限があるため、派生指標の表示に留める。
- FRED APIキーは環境変数から読む（コードに直接書かない＝GitHub Secretsで管理）。
- 金は LBMA系列（FRED）が廃止のため、GLD(Stooq→Yahoo)をフォールバック付きで取得。
- 取得失敗時もサイト全体が壊れないよう、各系列ごとに例外を握りつぶして記録する。

実行方法:
    export FRED_API_KEY=あなたのキー
    python3 fetch_and_build.py
"""

import os
import json
import time
import datetime as dt
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# ------------------------------------------------------------------
# 追跡する系列の定義
#   source     : "fred" または "stooq"（金のみStooq→Yahooフォールバック）
#   id         : FREDの系列IDまたはStooqシンボル
#   yahoo_symbol: Yahooフォールバック用シンボル（stooq系列のみ）
#   label      : 画面表示名
#   asset      : 資産クラス（oil/gold/equity/fx/bond）
#   restricted : Trueなら再配布制限あり → 生値は出さず変化率のみ表示
# ------------------------------------------------------------------
SERIES = [
    {"source": "fred",  "id": "DCOILWTICO",   "label": "WTI原油先物",   "asset": "oil",    "restricted": False},
    {"source": "fred",  "id": "DCOILBRENTEU", "label": "Brent原油先物", "asset": "oil",    "restricted": False},
    {"source": "fred",  "id": "VIXCLS",       "label": "VIX(恐怖指数)", "asset": "equity", "restricted": False},
    {"source": "fred",  "id": "DGS10",        "label": "米10年債利回り","asset": "bond",   "restricted": False},
    {"source": "fred",  "id": "DEXJPUS",      "label": "ドル円",        "asset": "fx",     "restricted": False},
    {"source": "fred",  "id": "SP500",        "label": "S&P500",        "asset": "equity", "restricted": True},
    # 金: LBMA系列(FRED)は廃止のため GLD(Stooq→Yahoo)を金価格の代理指標として使用
    {"source": "stooq", "id": "gld.us", "yahoo_symbol": "GLD",
     "label": "金(GLD)", "asset": "gold", "restricted": True},
]

LOOKBACK_DAYS = 30


# ---------- FRED 取得 ----------
def fetch_fred_series(series_id, days=120):
    """FREDから直近観測値を取得して [(date, value), ...] を返す。"""
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY が設定されていません")
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    params = {"series_id": series_id, "api_key": FRED_API_KEY,
              "file_type": "json", "observation_start": start, "sort_order": "asc"}
    url = f"{FRED_BASE}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "MarketcastLab/0.1"})
    with urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out = []
    for obs in payload.get("observations", []):
        v = obs.get("value", ".")
        if v not in (".", "", None):
            try:
                out.append((obs["date"], float(v)))
            except ValueError:
                pass
    return out


# ---------- Stooq 取得 ----------
def fetch_stooq_series(symbol, days=120):
    """Stooqから日次終値を [(date, value), ...] で返す。"""
    start = dt.date.today() - dt.timedelta(days=days)
    end = dt.date.today()
    params = {"s": symbol, "d1": start.strftime("%Y%m%d"),
              "d2": end.strftime("%Y%m%d"), "i": "d"}
    url = f"https://stooq.com/q/d/l/?{urlencode(params)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/csv,text/plain,*/*",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8")
    lines = text.strip().splitlines()
    if not lines or not lines[0].lower().startswith("date"):
        if "<!doctype" in text.lower() or "<html" in text.lower():
            raise RuntimeError("Stooqにブロックされました（HTML応答）")
        raise RuntimeError(f"有効なCSVヘッダなし（先頭: {text[:60]!r}）")
    header = lines[0].split(",")
    close_idx = next((i for i, h in enumerate(header) if h.lower() == "close"), 4)
    out = []
    for line in lines[1:]:
        cols = line.split(",")
        if len(cols) <= close_idx:
            continue
        try:
            out.append((cols[0], float(cols[close_idx])))
        except ValueError:
            pass
    if not out:
        raise RuntimeError("CSVは取得したがデータ行が空")
    return out


# ---------- Yahoo Finance フォールバック ----------
def fetch_yahoo_series(symbol, days=120):
    """Yahoo FinanceからGLD等を取得（Stooq失敗時のフォールバック）。"""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        df = ticker.history(start=start, interval="1d", auto_adjust=True)
        out = []
        if df is not None and len(df) > 0:
            for idx, row in df.iterrows():
                d = idx.date().isoformat() if hasattr(idx, 'date') else str(idx)[:10]
                out.append((d, float(row["Close"])))
        if out:
            print(f"  [Yahoo/yfinance] {symbol} 取得成功: {len(out)}件")
            return out
        raise RuntimeError("yfinanceの応答が空")
    except ImportError:
        pass
    except Exception as e:
        print(f"  [Yahoo/yfinance] {symbol} 失敗: {e} → chart APIへ")

    # Yahoo chart API 直叩き
    period1 = int(dt.datetime(*(dt.date.today() - dt.timedelta(days=days)).timetuple()[:3],
                              tzinfo=dt.timezone.utc).timestamp())
    period2 = int(dt.datetime(*dt.date.today().timetuple()[:3],
                              tzinfo=dt.timezone.utc).timestamp()) + 86400
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={period1}&period2={period2}&interval=1d&events=history")
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
               "Accept": "application/json"}
    req = Request(url, headers=headers)
    with urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0].get("close", [])
    out = [(dt.datetime.utcfromtimestamp(ts).date().isoformat(), float(c))
           for ts, c in zip(timestamps, closes) if c is not None]
    if not out:
        raise RuntimeError("Yahoo chart APIの応答が空")
    print(f"  [Yahoo/chart API] {symbol} 取得成功: {len(out)}件")
    return out


# ---------- ソース振り分け ----------
def fetch_series(s, days=120):
    """series定義に応じて取得元を振り分ける。"""
    if s["source"] == "stooq":
        try:
            return fetch_stooq_series(s["id"], days)
        except (URLError, HTTPError, RuntimeError) as e:
            print(f"  [Stooq→Yahoo フォールバック] {s['id']}: {e}")
            return fetch_yahoo_series(s.get("yahoo_symbol", "GLD"), days)
    return fetch_fred_series(s["id"], days)


# ---------- 変化率計算 ----------
def compute_trend(observations):
    """直近値と30日前の値から変化率・方向を計算する。"""
    if len(observations) < 2:
        return None
    latest_date, latest_val = observations[-1]
    target = dt.date.fromisoformat(latest_date) - dt.timedelta(days=LOOKBACK_DAYS)
    base_date, base_val = observations[0]
    for d, v in observations:
        if dt.date.fromisoformat(d) <= target:
            base_date, base_val = d, v
        else:
            break
    if base_val == 0:
        return None
    pct = (latest_val - base_val) / abs(base_val) * 100.0
    direction = "up" if pct > 0.3 else ("down" if pct < -0.3 else "flat")
    return {"as_of": latest_date, "base_date": base_date,
            "pct_change": round(pct, 1), "direction": direction}


# ---------- メイン ----------
def build_payload():
    """全系列を処理して market.json 用の最終JSON構造を組み立てる。"""
    results = []
    for s in SERIES:
        entry = {"label": s["label"], "asset": s["asset"], "status": "ok"}
        try:
            obs = fetch_series(s)
            trend = compute_trend(obs)
            if trend is None:
                entry["status"] = "no_data"
            else:
                entry.update(trend)
                if not s["restricted"]:
                    entry["latest_value"] = obs[-1][1]
                entry["restricted"] = s["restricted"]
        except (URLError, HTTPError, RuntimeError) as e:
            entry["status"] = "error"
            entry["error"] = str(e)
            print(f"  [エラー] {s['label']}: {e}")
        results.append(entry)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "series": results,
        "disclaimer": "本データは公開情報に基づく一般的・教育的な情報提供であり、投資助言ではありません。",
    }


if __name__ == "__main__":
    payload = build_payload()
    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "market.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {out_path}")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:800])

