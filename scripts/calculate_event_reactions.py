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


def _safe_error_message(error):
    """例外メッセージから FRED_API_KEY を除去して返す。
    HTTPError 等の例外文字列にAPIキーを含むURLが混入することを防ぐ。
    ログ出力・JSON保存の両方で使用すること。
    """
    message = str(error)
    api_key = os.environ.get("FRED_API_KEY", "")
    if api_key:
        message = message.replace(api_key, "***")
    return message[:300]

HERE = os.path.dirname(__file__)
EVENTS_PATH = os.path.join(HERE, "..", "data", "events.json")
GROUP_META_PATH = os.path.join(HERE, "..", "data", "group_metadata.json")
OUT_PATH = os.path.join(HERE, "..", "data", "event_reactions.json")
MARKET_PATH = os.path.join(HERE, "..", "data", "market.json")
CURRENT_CONTEXT_PATH = os.path.join(HERE, "..", "data", "current_context_public.json")

# 対象資産。source=fred は FRED API、source=stooq は Stooq から取得。
# restricted=Trueは再配布制限があるため生値を出さず変化率/派生値のみ表示。
# 金は LBMA系列(FRED)が更新停止のため、金ETF GLD(Stooq)を金価格の代理指標として使用する。
ASSETS = [
    {"key": "wti",    "label": "WTI原油",      "source": "fred",  "series": "DCOILWTICO", "asset": "oil",    "restricted": False},
    {"key": "gold",   "label": "金（GLD）",     "source": "stooq", "series": "gld.us",     "yahoo_symbol": "GLD", "asset": "gold",   "restricted": True},
    {"key": "sp500",  "label": "S&P500",        "source": "fred",  "series": "SP500",      "yahoo_symbol": "^GSPC", "yahoo_auto_adjust": False, "asset": "equity", "restricted": True},
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

# ===== market_state_tags 共通分類関数 =====
# 現在側・過去イベント側の両方が必ずこれらを使う。閾値は仕様として固定。

# CSN Step1 に実在する8分類（index.html から確認済み）
CSN_CAUSE_TAGS = [
    "supply_shock", "bank_crisis", "war", "middle_east",
    "monetary_tightening", "monetary_easing", "emergency_cut", "pandemic",
]

def classify_vix(value):
    """VIX水準を帯タグ化。None → None。"""
    if value is None:
        return None
    if value < 15:
        return "calm"
    if value < 25:
        return "elev"
    if value < 40:
        return "stress"
    return "panic"


def classify_oil(value):
    """WTI原油水準を帯タグ化。None → None。"""
    if value is None:
        return None
    if value < 40:
        return "lo"
    if value < 80:
        return "mid"
    return "hi"


def classify_rate(ff, ust10y):
    """FF金利と10年債利回りの単純平均で金利環境を帯タグ化。
    片方欠損時は存在する値のみ利用。両方欠損時は None。"""
    vals = [v for v in [ff, ust10y] if v is not None]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    if avg < 2:
        return "low"
    if avg < 4:
        return "mid"
    return "high"


def build_market_state_tags(vix, oil, ff, ust10y):
    """4指標値から market_state_tags dict を生成。タグなし軸は含めない。"""
    tags = {}
    v = classify_vix(vix)
    if v is not None:
        tags["vix"] = v
    o = classify_oil(oil)
    if o is not None:
        tags["oil"] = o
    r = classify_rate(ff, ust10y)
    if r is not None:
        tags["rate"] = r
    return tags


# モジュールレベルの系列キャッシュ（1回のActions実行中に全関数で共有）
# series_id をキーとして全期間データを保持し、重複取得を防ぐ。
# 異なる series_id のデータが混在しないよう、キーは series_id のみとする。
_SERIES_CACHE: dict = {}

# キャッシュで取得する全期間（全イベントをカバーする最大窓）
_CACHE_START = dt.date(1950, 1, 1)
_CACHE_END   = dt.date.today() + dt.timedelta(days=120)


def fetch_series_range(series_id, start, end):
    """FRED から指定期間の観測値を {date: value} で返す。
    HTTP 429 は指数バックオフ（2/4/8秒）で最大3回リトライする。
    3回失敗した場合は例外として上位へ返し、status=error にする（no_data には変換しない）。
    APIキー・完全URL・クエリ文字列はログに出さない。
    """
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
    last_err = None
    for attempt in range(1, 4):
        try:
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
        except HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt  # 2, 4, 8秒
                print(f"  [FRED] {series_id} 429 Rate Limited — {wait}秒待機 (試行{attempt}/3)")
                time.sleep(wait)
                last_err = e
            else:
                raise
    raise RuntimeError(f"FRED 429 リトライ上限超過: {series_id} / {type(last_err).__name__}")


def fetch_series_range_cached(series_id, start, end):
    """系列単位で全期間データをキャッシュし、窓を切り出して返す。
    1回のActions実行中に同一series_idの呼び出しは原則1回のFRED取得で済む。
    キャッシュは _SERIES_CACHE に series_id をキーとして格納し、
    異なる series_id 間でデータが混在しない。
    """
    if series_id not in _SERIES_CACHE:
        print(f"  [FRED cache] {series_id} 全期間取得 ({_CACHE_START} 〜 {_CACHE_END})")
        _SERIES_CACHE[series_id] = fetch_series_range(series_id, _CACHE_START, _CACHE_END)
        print(f"  [FRED cache] {series_id} {len(_SERIES_CACHE[series_id])}件 キャッシュ完了")
    else:
        print(f"  [FRED cache] {series_id} キャッシュ再利用")
    full = _SERIES_CACHE[series_id]
    s, e = start.isoformat(), end.isoformat()
    return {d: v for d, v in full.items() if s <= d <= e}


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
            print(f"  [Stooq] {symbol} 取得失敗 (試行{attempt}/3): {type(e).__name__}: {_safe_error_message(e)}")
            time.sleep(2 * attempt)
        except (URLError, HTTPError) as e:
            # 接続エラーはリトライする価値あり
            last_err = e
            print(f"  [Stooq] {symbol} 接続エラー (試行{attempt}/3): {type(e).__name__}: {_safe_error_message(e)}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"Stooq取得に3回失敗: {symbol} / 最終エラー: {last_err}")


def fetch_yahoo_range(symbol, start, end, auto_adjust=True):
    """Yahoo Financeから日次終値を {date: value} で返す（Stooq失敗時のフォールバック）。
    auto_adjust=True（デフォルト）は金GLD用の既存挙動を維持する。
    S&P500フォールバック時は auto_adjust=False を明示して呼び出すこと。
    yfinanceがあれば使い、無ければYahooのchart APIを直接叩く。"""
    # まず yfinance を試す（0.2系以降のAPI対応）
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start.isoformat(),
                            end=(end + dt.timedelta(days=1)).isoformat(),
                            interval="1d", auto_adjust=auto_adjust)
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
        print(f"  [Yahoo/yfinance] {symbol} 取得失敗: {type(e).__name__} → chart APIを試行")

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
            print(f"  [フォールバック] {asset['key']}: Stooq失敗のためYahooへ切替 ({type(e).__name__})")
            ysym = asset.get("yahoo_symbol", asset["series"].replace(".us", "").upper())
            return fetch_yahoo_range(ysym, start, end), "yahoo"
    return fetch_series_range_cached(asset["series"], start, end), "fred"


def value_on_or_before(series_map, target_date, max_lookback=7):
    """target_date 当日、なければ直前の取引日の値を返す（最大max_lookback日遡る）。"""
    for back in range(0, max_lookback + 1):
        d = (target_date - dt.timedelta(days=back)).isoformat()
        if d in series_map:
            return series_map[d], d
    return None, None


def _fill_changes(entry, series_map, base_val, used_base_date, base_date, is_yield):
    """変化率計算を entry に書き込む共通ヘルパー（FRED / Yahoo 共用）。"""
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


def _fetch_sp500_yahoo_fallback(asset, start, end, base_date):
    """SP500 専用 Yahoo ^GSPC フォールバック取得ヘルパー。
    取得・基準値解決のみ行い、(series_map, base_val, used_base_date) を返す。
    status / source / error の書き込みは呼び出し元 compute_reactions_for_event で行う。
    auto_adjust は ASSETS の yahoo_auto_adjust 設定に従う（probe 検証済み: False で FRED と一致）。
    """
    auto_adj = asset.get("yahoo_auto_adjust", False)
    yahoo_map = fetch_yahoo_range(asset["yahoo_symbol"], start, end, auto_adjust=auto_adj)
    base_val, used_base_date = value_on_or_before(yahoo_map, base_date)
    return yahoo_map, base_val, used_base_date


def compute_reactions_for_event(event):
    """1イベントについて、各資産の各期間の変化率を計算。
    SP500 は FRED を一次ソースとし、FRED で基準値が得られない場合（範囲外・空・例外）に
    Yahoo Finance ^GSPC を二次ソースとして使用する（probe 検証: 2026-06-19 Run#1）。
    """
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
                # FRED 取得成功でも基準値が得られない場合（範囲外・空）
                # → SP500 のみ Yahoo ^GSPC フォールバック（gold の Stooq→Yahoo とは別経路）
                if a["key"] == "sp500" and a.get("yahoo_symbol"):
                    print(f"  [sp500] FRED基準値なし → Yahoo {a['yahoo_symbol']} でフォールバック")
                    try:
                        yahoo_map, yahoo_bv, yahoo_bd = _fetch_sp500_yahoo_fallback(
                            a, start, end, base_date)
                        entry["source"] = "yahoo"
                        entry["fallback_from"] = "fred"
                        if yahoo_bv is None or yahoo_bv == 0:
                            entry["status"] = "no_data"
                        else:
                            _fill_changes(entry, yahoo_map, yahoo_bv, yahoo_bd, base_date, is_yield)
                    except (URLError, HTTPError, RuntimeError) as ye:
                        entry["source"] = "yahoo"
                        entry["fallback_from"] = "fred"
                        entry["status"] = "error"
                        entry["error"] = _safe_error_message(ye)
                else:
                    entry["status"] = "no_data"
            else:
                _fill_changes(entry, series_map, base_val, used_base_date, base_date, is_yield)
        except (URLError, HTTPError, RuntimeError) as e:
            # FRED 自体が例外 → SP500 のみ Yahoo フォールバック
            if a["key"] == "sp500" and a.get("yahoo_symbol"):
                print(f"  [sp500] FRED例外 ({type(e).__name__}) → Yahoo {a['yahoo_symbol']} でフォールバック")
                try:
                    yahoo_map, yahoo_bv, yahoo_bd = _fetch_sp500_yahoo_fallback(
                        a, start, end, base_date)
                    entry["source"] = "yahoo"
                    entry["fallback_from"] = "fred"
                    if yahoo_bv is None or yahoo_bv == 0:
                        entry["status"] = "no_data"
                    else:
                        _fill_changes(entry, yahoo_map, yahoo_bv, yahoo_bd, base_date, is_yield)
                except (URLError, HTTPError, RuntimeError) as ye:
                    entry["source"] = "yahoo"
                    entry["fallback_from"] = "fred"
                    entry["status"] = "error"
                    entry["error"] = _safe_error_message(ye)
            else:
                entry["status"] = "error"
                entry["error"] = _safe_error_message(e)
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


def build_current_context(er_payload):
    """
    current_context_public.json を生成する。
    market.json（VIX/油/10年債）と _SERIES_CACHE["DFF"]（FF金利）を統合し、
    現在の market_state_tags と cause別無料top1を出力する。

    設計原則:
    - 有料加工結果（候補2-5件・全一致/不一致・6資産反応）は含めない
    - DFF取得失敗時も既存のevent_reactions.json生成は継続済みのため、
      ここでは失敗を捕捉してフォールバックするだけでよい
    - 現在・過去で必ず同じ build_market_state_tags を使用
    - 日時はすべてJST基準（UTC+9）で統一
    """
    # JST 基準の「今日」: GitHub Actions は UTC 動作のため date.today() は UTC 日付になる。
    # UTC+9 で統一することで日付境界付近の1日ズレを防ぐ。
    _JST = dt.timezone(dt.timedelta(hours=9))
    today_jst = dt.datetime.now(_JST).date()

    # ---------- 1. market.json からVIX・油・10年債を取得 ----------
    # market.json の各系列を取得するための明示的マッピング。
    # Git版(idなし)では asset + label完全一致で取得。曖昧な部分一致は使わない。
    # asset="equity" は VIX と S&P500 の2件存在するため label も必ず照合する。
    # 安全に特定できない場合は unavailable とする。
    _MARKET_SERIES_MAP = {
        # series_id: (asset値, label完全一致文字列)
        "VIXCLS":     ("equity", "VIX(恐怖指数)"),
        "DCOILWTICO": ("oil",    "WTI原油先物"),
        "DGS10":      ("bond",   "米10年債利回り"),
    }

    def _load_market():
        try:
            with open(MARKET_PATH, encoding="utf-8") as f:
                mkt = json.load(f)
        except Exception as e:
            print(f"  [current_context] market.json 読み込み失敗: {e}")
            return {}, []
        series_list = mkt.get("series", [])
        by_id = {}
        for s in series_list:
            if isinstance(s, dict) and s.get("id"):
                by_id[s["id"]] = s
        return by_id, series_list

    by_id, series_list = _load_market()

    def _get_series(series_id):
        """id → フォールバック(asset完全一致+label完全一致) の順で market series を取得。
        曖昧な部分一致は使わない。安全に特定できなければ None を返す。"""
        s = by_id.get(series_id)
        if s:
            return s
        # Git版フォールバック: _MARKET_SERIES_MAP に定義された asset + label 完全一致のみ
        mapping = _MARKET_SERIES_MAP.get(series_id)
        if mapping is None:
            return None
        expected_asset, expected_label = mapping
        for item in series_list:
            if not isinstance(item, dict):
                continue
            if item.get("asset") == expected_asset and item.get("label") == expected_label:
                return item
        return None

    def _indicator(s):
        """market.json の series エントリ → indicators エントリ。stale判定はJST基準。
        - value なし / as_of なし / as_of 解析不能 → status="unavailable"
        - 有効な as_of で 8暦日以上前                → status="stale"
        - 有効な as_of で 7暦日以内                  → status="ok"
        """
        if s is None or s.get("latest_value") is None:
            return {"value": None, "as_of": None, "status": "unavailable"}
        val = s["latest_value"]
        as_of_str = s.get("as_of")
        if not as_of_str:
            return {"value": None, "as_of": None, "status": "unavailable"}
        try:
            delta = (today_jst - dt.date.fromisoformat(as_of_str)).days
            status = "ok" if delta <= 7 else "stale"
        except ValueError:
            # as_of が ISO 日付として解析不能 → unavailable
            return {"value": None, "as_of": None, "status": "unavailable"}
        return {"value": val, "as_of": as_of_str, "status": status}

    ind_vix    = _indicator(_get_series("VIXCLS"))
    ind_oil    = _indicator(_get_series("DCOILWTICO"))
    ind_ust10y = _indicator(_get_series("DGS10"))

    # ---------- 2. _SERIES_CACHE["DFF"] から FF 現在値を取得 ----------
    # DFF は build() 内の先行ロードで _SERIES_CACHE に登録済み（失敗時は空dict）。
    # 空dictの場合も dff_cache が falsy なので unavailable に落ちる。
    ff_val = None
    ff_as_of = None
    ff_status = "unavailable"
    try:
        dff_cache = _SERIES_CACHE.get("DFF", {})
        if dff_cache:
            # JST基準の今日以前の最新値を取得（最大7日遡及）
            ff_val, ff_date_str = value_on_or_before(dff_cache, today_jst, max_lookback=7)
            if ff_val is not None:
                ff_val = round(ff_val, 4)
                ff_as_of = ff_date_str
                delta = (today_jst - dt.date.fromisoformat(ff_date_str)).days
                ff_status = "ok" if delta <= 7 else "stale"
            else:
                print("  [current_context] DFF: キャッシュあるが今日前後7日の値なし → unavailable")
        else:
            print("  [current_context] DFF: キャッシュ未取得 → unavailable (金利環境は10年債のみでフォールバック)")
    except Exception as e:
        print(f"  [current_context] DFF 現在値取得中に例外: {e}")

    ind_ff = {"value": ff_val, "as_of": ff_as_of, "status": ff_status}

    # ---------- 3. market_state_tags 生成 ----------
    vix_val    = ind_vix["value"]
    oil_val    = ind_oil["value"]
    ust10y_val = ind_ust10y["value"]

    state_tags = build_market_state_tags(vix_val, oil_val, ff_val, ust10y_val)
    data_completeness = len(state_tags)  # 0〜3

    # ---------- 4. rate_display 生成 ----------
    rate_labels = {"low": "低", "mid": "中", "high": "高"}
    rate_env_label = rate_labels.get(state_tags.get("rate"))  # rate タグなし → None（画面側で "--" に変換）

    spread = None
    curve_note = None
    if ff_val is not None and ust10y_val is not None:
        spread = round(ust10y_val - ff_val, 3)
        if spread < -0.3:
            curve_note = "inverted"
        elif spread < 0.3:
            curve_note = "flat"
        else:
            curve_note = "normal"

    rate_display = {
        "rate_env_label": rate_env_label,
        "spread": spread,
        "curve_note": curve_note,
    }

    # ---------- 5. free_top_match 生成 ----------
    # er_payload["events"] の context_snapshot を使い、
    # cause別にグループ内での state_tags 一致数でtop1を選ぶ。
    # 現在と過去で必ず同じ classify_*/build_market_state_tags を使用する。
    # oil_shock_1973 は比較可能軸2未満のためランキング対象外（通常除外）。
    # data_completeness < 2 のとき全グループで「明確な類似局面なし」とする。

    RANK_EXCLUDED = {"oil_shock_1973"}  # ランキング対象外（参考イベント）

    def _make_matched_summary(matched_axes):
        """一致した軸のリストから人間が読めるサマリー文字列を生成。"""
        labels = {"vix": "VIX水準", "oil": "原油水準", "rate": "金利環境"}
        parts = [labels.get(a, a) for a in matched_axes]
        if not parts:
            return "一致する市場環境なし"
        return "・".join(parts) + "が一致"

    events_for_match = er_payload.get("events", [])

    def _top1_for_cause(cause_tag):
        """cause_tag グループ内のtop1候補を返す。なければNoneを返す。"""
        group = [
            e for e in events_for_match
            if cause_tag in (e.get("cause_tags") or [])
            and e["id"] not in RANK_EXCLUDED
        ]
        if not group:
            return None

        scored = []
        for ev in group:
            cs = ev.get("context_snapshot") or {}
            ev_tags = build_market_state_tags(
                cs.get("vix_level"),
                cs.get("oil_price_wti"),
                cs.get("fed_funds_rate"),
                cs.get("ust10y_yield"),
            )
            # 両者に共通して存在する軸のみ比較
            common_axes = set(state_tags) & set(ev_tags)
            if len(common_axes) < 2:
                continue  # 比較可能軸2未満は除外
            matched = [ax for ax in common_axes if state_tags[ax] == ev_tags[ax]]
            n_match = len(matched)
            n_comp  = len(common_axes)
            # 発生日（新しい順で同点解消）: ISO文字列の降順
            scored.append((n_match, n_comp, ev["date"], matched, ev))

        if not scored:
            return None

        # ソート: 一致数↓ → 比較可能数↓ → 発生日↓（新しい順）
        # ISO日付文字列(YYYY-MM-DD)を整数に変換して負値にすることで降順化
        def _date_key(d):
            try:
                return -int(d.replace("-", ""))
            except Exception:
                return 0

        scored.sort(key=lambda x: (-x[0], -x[1], _date_key(x[2])))
        best = scored[0]
        n_match, n_comp, _, matched_axes, best_ev = best

        # 重なりラベル判定
        if n_match == 3:
            overlap_label = "高い重なり"
        elif n_match >= 1 and n_match > n_comp / 2:
            overlap_label = "中程度の重なり"
        elif n_match == 1:
            overlap_label = "部分的な重なり"
        else:
            overlap_label = "明確な類似局面なし"

        # 無料top1の表示条件:
        # 「高い重なり」「中程度の重なり」のみ event_id を返す。
        # 「部分的な重なり」以下は候補はあるが重なり不足 → event_id=null で返す。
        if overlap_label in ("高い重なり", "中程度の重なり"):
            return {
                "event_id":        best_ev["id"],
                "name":            best_ev["name"],
                "date":            best_ev["date"],
                "overlap_label":   overlap_label,
                "matched_summary": _make_matched_summary(matched_axes),
                "cta":             "有料版で類似局面の詳細比較を見る",
            }
        else:
            # 候補はあるが中程度以上の重なりなし
            return {
                "event_id":        None,
                "overlap_label":   "明確な類似局面なし",
                "matched_summary": "現在の市場環境と中程度以上に重なる過去局面はありません",
            }

    free_top_match = {}
    for cause_tag in CSN_CAUSE_TAGS:
        if data_completeness < 2:
            free_top_match[cause_tag] = {
                "event_id": None,
                "overlap_label": "明確な類似局面なし",
                "matched_summary": "現在の市場環境データが不足しているため比較できません",
            }
        else:
            result = _top1_for_cause(cause_tag)
            if result is None:
                free_top_match[cause_tag] = {
                    "event_id": None,
                    "overlap_label": "明確な類似局面なし",
                    "matched_summary": "該当する比較対象がありません",
                }
            else:
                free_top_match[cause_tag] = result

    # ---------- 6. 出力 ----------
    now_jst = dt.datetime.now(_JST)

    context_payload = {
        "generated_at": now_jst.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "indicators": {
            "vix":    ind_vix,
            "oil":    ind_oil,
            "ff":     ind_ff,
            "ust10y": ind_ust10y,
        },
        "market_state_tags": state_tags,
        "rate_display": rate_display,
        "data_completeness": data_completeness,
        "free_top_match": free_top_match,
        "disclaimer": "本データは過去の市場状況の記録であり、将来の値動きを示すものでも、売買を推奨するものでもありません。",
        "data_note": "各指標の基準日はデータ提供元の更新タイミングにより異なります。CPI等の月次データはこのファイルに含まれません。",
    }

    try:
        with open(CURRENT_CONTEXT_PATH, "w", encoding="utf-8") as f:
            json.dump(context_payload, f, ensure_ascii=False, indent=2)
        print(f"[current_context] 書き出し完了: {CURRENT_CONTEXT_PATH}")
    except Exception as e:
        print(f"  [current_context] 書き出し失敗: {e}")


def build():
    with open(EVENTS_PATH, encoding="utf-8") as f:
        events_master = json.load(f)

    effect_rules = events_master.get("effect_rules", {})
    conf_levels = events_master.get("confidence_levels", [])

    # ===== Task 3: context_snapshot 自動入力 =====
    # events.jsonでnullの項目をFREDから取得して補完する
    # CPI(cpi_yoy)は月次のため対象外（手動入力を維持）
    CS_SERIES = {
        "vix_level":     "VIXCLS",
        "oil_price_wti": "DCOILWTICO",
        "fed_funds_rate": "DFF",
        "ust10y_yield":  "DGS10",
    }

    # DFF 無条件先行ロード:
    # auto_fill は cs.get("fed_funds_rate") が非 None のとき DFF 取得をスキップするため、
    # 全19件のFF金利が充填済みの本番では DFF が _SERIES_CACHE に登録されない。
    # current_context 生成で DFF 現在値が必要なため、ここで最大1回だけ確実に登録する。
    # キャッシュ済みなら fetch_series_range_cached が再取得せずキャッシュを再利用する。
    # 【失敗時も空dictをキャッシュ】: 失敗後に auto_fill が fed_funds_rate=None の
    # イベントに対してDFFを再試行しないよう、失敗結果も _SERIES_CACHE["DFF"]={} で確定する。
    if "DFF" not in _SERIES_CACHE:
        try:
            fetch_series_range_cached("DFF", _CACHE_START, _CACHE_END)
        except Exception as e:
            print(f"  [build] DFF 先行ロード失敗: {_safe_error_message(e)} → 空キャッシュを登録し再試行を防止")
            _SERIES_CACHE["DFF"] = {}  # 失敗結果をキャッシュ: 以後 auto_fill も再試行しない

    def fetch_cs_value(series_id, base_date):
        """イベント日時点のFRED値を取得（モジュールレベルの_SERIES_CACHEを共有）。
        _cs_cache.clear() は廃止し、全イベントで同一キャッシュを再利用する。"""
        try:
            series_map = fetch_series_range_cached(series_id,
                                                    base_date - dt.timedelta(days=15),
                                                    base_date + dt.timedelta(days=3))
        except Exception as e:
            print(f"  [context_snapshot] {series_id} 取得失敗: {type(e).__name__}: {_safe_error_message(e)}")
            return None
        val, _ = value_on_or_before(series_map, base_date)
        return round(val, 4) if val is not None else None

    def auto_fill_context_snapshot(ev):
        """
        events.jsonのcontext_snapshotがnullの項目をFREDから自動補完。
        既入力の値は上書きしない。cpi_yoyは手動入力のため対象外。
        """
        cs = dict(ev.get("context_snapshot") or {})
        # 5項目を確実に保持
        for f in ["vix_level","oil_price_wti","cpi_yoy","fed_funds_rate","ust10y_yield"]:
            cs.setdefault(f, None)

        base_date = dt.date.fromisoformat(ev["date"])
        # nullの項目だけ取得（キャッシュはイベントをまたいで共有される）
        for field, series_id in CS_SERIES.items():
            if cs.get(field) is None:
                cs[field] = fetch_cs_value(series_id, base_date)
                if cs[field] is not None:
                    print(f"  [context_snapshot] {ev['id']} {field}={cs[field]} (自動取得)")
        return cs

    events_with_reactions = []
    for ev in events_master["events"]:
        reactions = compute_reactions_for_event(ev)
        effect_tags, effect_tag_details = detect_effect_tags(reactions, effect_rules)

        # context_snapshot: nullをFREDで自動補完
        filled_cs = auto_fill_context_snapshot(ev)

        events_with_reactions.append({
            "id": ev["id"],
            "name": ev["name"],
            "date": ev["date"],
            "category": ev.get("category"),
            "cause_tags": ev.get("cause_tags", []),
            "effect_tags": effect_tags,
            "effect_tag_details": effect_tag_details,
            "causal_chain": ev.get("causal_chain", []),
            "context_snapshot": filled_cs,
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

    # グループメタ情報を合流
    group_meta = {}
    if os.path.exists(GROUP_META_PATH):
        with open(GROUP_META_PATH, encoding="utf-8") as f:
            group_meta = json.load(f).get("groups", {})

    # ===== Task 2: reverse_contrast 逆引きインデックス生成 =====
    # 「AがBをwithとして参照している」とき、B側に逆参照を追加。
    # this_side / other_side を逆転させることでB視点のcontrastとして表示できる。
    reverse_contrast = {}  # { referenced_id: [{from_id, axis, this_side, other_side, outcome_note, type}] }
    for ev_r in events_with_reactions:
        for c in ev_r.get("contrast", []):
            target_id = c.get("with")
            if not target_id:
                continue
            if target_id not in reverse_contrast:
                reverse_contrast[target_id] = []
            reverse_contrast[target_id].append({
                "from": ev_r["id"],
                "from_name": ev_r["name"],
                "axis": c.get("axis", ""),
                # 視点を逆転（B側から見ると this/other が入れ替わる）
                "this_side": c.get("other_side", ""),
                "other_side": c.get("this_side", ""),
                "outcome_note": c.get("outcome_note", ""),
                "type": c.get("type", "structural_contrast"),
            })

    # events_with_reactions に reverse_contrast を付与
    rc_map = {e["id"]: e for e in events_with_reactions}
    for eid, rc_list in reverse_contrast.items():
        if eid in rc_map:
            rc_map[eid]["reverse_contrast"] = rc_list

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

    # ===== S2: current_context_public.json 生成 =====
    build_current_context(payload)
