#!/usr/bin/env python3
"""S&P500 フォールバック候補ソース【検証専用】スクリプト (KI-001 / T-004,005,007)
本番非影響: probe_out/ のみ書き込む。market.json / event_reactions.json は触らない。
個々の候補取得失敗 → レポート記録して続行 / JSON・CSV生成失敗 → sys.exit(1)。
FRED_API_KEY はログ・レポートに出力しない（safe_error() で必ずマスクする）。
"""

import os, sys, csv, json, time, statistics, datetime as dt
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

# -- 設定
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")   # ログ出力禁止
FRED_BASE    = "https://api.stlouisfed.org/fred/series/observations"
HERE         = os.path.dirname(os.path.abspath(__file__))
OUT_DIR      = os.path.join(HERE, "..", "probe_out")
os.makedirs(OUT_DIR, exist_ok=True)

# 現行コードと同じ期間定義（05_CALCULATION_LOGIC.md / HORIZONS）
HORIZONS     = [("d1",1),("d7",7),("d30",30),("d90",90)]
MAX_LOOKBACK = 7

# 暫定判定閾値（実装前に正式決定する）
T_PRICE_MED   = 0.5    # 価格水準差 絶対差中央値（%）
T_CHANGE_MED  = 0.1    # 変化率差 中央値（%pt）
T_CHANGE_MAX  = 0.5    # 変化率差 最大絶対値（%pt）
MIN_PRICE_SAMPLE_N = 30  # 価格差比較の最低共通日数

# 検証終了日: dt.date.today() を使用して最新データまで取得する
# （再現性より最新の取得可否確認を優先するため。実行日により結果が変わる点に注意）
PROBE_END = dt.date.today()

# FRED 範囲外候補 7 イベント（events.json から確認済み）
OLD_EVENTS = [
    ("1973-10-17","oil_shock_1973"),    ("1990-08-02","gulf_war_1990"),
    ("1997-07-02","asian_crisis_1997"), ("1998-09-02","ltcm_crisis_1998"),
    ("2008-09-15","lehman_shock_2008"), ("2010-04-27","eurozone_crisis_2010"),
    ("2011-02-17","libya_civil_war_2011"),
]
# FRED 範囲内イベント（変化率差の比較用）
RECENT_EVENTS = [
    ("2020-03-09","covid_shock_2020"),  ("2022-02-24","russia_ukraine_2022"),
    ("2023-03-10","svb_collapse_2023"), ("2024-04-13","iran_israel_strike_2024"),
]
# 候補シンボル（実取得結果でのみ採否を判断する）
STOOQ_CANDIDATES = ["^spx","^gspc","spx","^sp500"]
YAHOO_CANDIDATES = ["^GSPC","^SP500","SPX"]   # yfinance / auto_adjust=False / Close


# -- セキュリティ: 例外メッセージから FRED_API_KEY をマスクする
def safe_error(e):
    """例外文字列中に FRED_API_KEY が含まれていればマスクして返す。
    URL クエリに API キーが含まれた HTTPError 等がログに漏れることを防ぐ。"""
    msg = str(e)
    if FRED_API_KEY:
        msg = msg.replace(FRED_API_KEY, "***")
    return msg[:200]


# -- ユーティリティ（現行 calculate_event_reactions.py と完全同一ロジック）
def value_on_or_before(m, d, lkb=MAX_LOOKBACK):
    for i in range(lkb+1):
        k = (d - dt.timedelta(days=i)).isoformat()
        if k in m: return m[k], k
    return None, None

def calc_changes(m, base):
    bv, _ = value_on_or_before(m, base)
    if not bv: return None
    ch = {}
    for h, days in HORIZONS:
        fv, _ = value_on_or_before(m, base + dt.timedelta(days=days))
        ch[h] = round((fv-bv)/abs(bv)*100.0, 1) if fv is not None else None
    return ch

def _med(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 4) if len(v) >= 2 else None

def _maxabs(vals):
    v = [x for x in vals if x is not None]
    return round(max(abs(x) for x in v), 4) if v else None


# -- データ取得
def fetch_fred(sid, start, end):
    if not FRED_API_KEY: raise RuntimeError("FRED_API_KEY 未設定")
    params = {"series_id":sid,"api_key":FRED_API_KEY,"file_type":"json",
              "observation_start":start.isoformat(),"observation_end":end.isoformat(),
              "sort_order":"asc"}
    req = Request(f"{FRED_BASE}?{urlencode(params)}", headers={"User-Agent":"MarketcastLab-Probe/0.1"})
    with urlopen(req, timeout=30) as r: payload = json.loads(r.read())
    out = {}
    for o in payload.get("observations",[]):
        try: out[o["date"]] = float(o["value"])
        except (ValueError, TypeError): pass
    return out

def fetch_stooq(sym, start, end):
    """Stooq CSV を取得して {data, blocked, http_status} を返す。"""
    params = {"s":sym,"d1":start.strftime("%Y%m%d"),"d2":end.strftime("%Y%m%d"),"i":"d"}
    req = Request(f"https://stooq.com/q/d/l/?{urlencode(params)}", headers={
        "User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept":"text/csv,*/*"})
    with urlopen(req, timeout=30) as r:
        status = r.getcode(); text = r.read().decode("utf-8","replace")
    blocked = "<!doctype" in text.lower() or "<html" in text.lower()
    data = {}
    if not blocked:
        lines = text.strip().splitlines()
        if lines and lines[0].lower().startswith("date"):
            hdr = [h.lower() for h in lines[0].split(",")]
            ci = hdr.index("close") if "close" in hdr else 4
            for ln in lines[1:]:
                cols = ln.split(",")
                try:
                    if len(cols) > ci: data[cols[0]] = float(cols[ci])
                except (ValueError, TypeError): pass
    return {"data":data,"blocked":blocked,"http_status":status}

def fetch_yahoo(sym, start, end):
    """yfinance(auto_adjust=False)でClose列を {date:close} で返す。"""
    import yfinance as yf
    df = yf.Ticker(sym).history(start=start.isoformat(),
                                end=(end+dt.timedelta(days=1)).isoformat(),
                                interval="1d", auto_adjust=False)
    if df is None or len(df) == 0: raise RuntimeError("yfinance 応答が空")
    out = {}
    for idx, row in df.iterrows():
        d = idx.date().isoformat() if hasattr(idx,"date") else str(idx)[:10]
        try: out[d] = float(row["Close"])
        except (ValueError, TypeError, KeyError): pass
    if not out: raise RuntimeError("Close 列からデータを取得できなかった")
    return out


# -- 候補 1 件の検証（Stooq / Yahoo 共通）
def probe_one(source, symbol, fred_overlap, fred_reactions):
    """取得・検証してレコードを返す（いかなる例外でも続行）。"""
    rec = {"source":source,"symbol":symbol,"success":False,"error_type":None,"error_msg":None,
           "http_status":None,"blocked":None,"data_start":None,"data_end":None,"data_n":0,
           "old_event_coverage":{},"price_diff":{},"change_diff":{},"verdict":{}}
    try:
        if source == "stooq":
            r = fetch_stooq(symbol, dt.date(1950,1,1), PROBE_END)
            rec["http_status"] = r["http_status"]; rec["blocked"] = r["blocked"]
            if r["blocked"]:
                rec["error_type"] = "html_blocked"
                rec["error_msg"]  = "HTML 応答（ブロック）"
                return rec
            data = r["data"]
        else:
            data = fetch_yahoo(symbol, dt.date(1950,1,1), PROBE_END)

        rec["data_n"] = len(data)
        if data:
            ds = sorted(data); rec["data_start"] = ds[0]; rec["data_end"] = ds[-1]
        if not data:
            rec["error_type"] = "empty_data"; rec["error_msg"] = "行数 0"; return rec

        rec["success"] = True

        # 古い 7 イベントのカバレッジ（取得率 100% が合格条件）
        for date_str, eid in OLD_EVENTS:
            bd = dt.date.fromisoformat(date_str)
            val, used = value_on_or_before(data, bd)
            rec["old_event_coverage"][eid] = {
                "target_date":date_str,"value":val,"used_date":used,"ok":val is not None}

        # 価格水準差（FRED 重複期間の直近 500 日）
        # 判定は符号付き差中央値ではなく絶対差中央値を使用する（符号の相殺を防ぐため）
        if fred_overlap:
            common = sorted(set(data) & set(fred_overlap))[-500:]
            diffs = [(data[d]-fred_overlap[d])/abs(fred_overlap[d])*100
                     for d in common if fred_overlap[d]]
            abs_diffs = [abs(x) for x in diffs]
            med_signed = _med(diffs)
            med_abs    = _med(abs_diffs)
            mabs       = _maxabs(diffs)
            pass_sample = len(diffs) >= MIN_PRICE_SAMPLE_N
            pass_median = med_abs is not None and med_abs <= T_PRICE_MED
            rec["price_diff"] = {
                "common_days":      len(common),
                "sample_n":         len(diffs),
                "median_signed_pct":med_signed,
                "median_abs_pct":   med_abs,
                "max_abs_pct":      mabs,
                "pass_sample_n":    pass_sample,
                "pass_median":      pass_median,
            }

        # 変化率差（RECENT_EVENTS × d1/d7/d30/d90）
        if fred_reactions:
            per_h = {h:[] for h,_ in HORIZONS}
            ev_detail = {}
            for date_str, eid in RECENT_EVENTS:
                fred_ch = fred_reactions.get(eid)
                if not fred_ch: continue
                cand_ch = calc_changes(data, dt.date.fromisoformat(date_str))
                if not cand_ch: continue
                row = {}
                for h,_ in HORIZONS:
                    fc, cc = fred_ch.get(h), cand_ch.get(h)
                    diff = round(cc-fc, 2) if (fc is not None and cc is not None) else None
                    row[h] = {"fred":fc,"cand":cc,"diff":diff}
                    if diff is not None: per_h[h].append(diff)
                ev_detail[eid] = row
            all_diffs = [d for v in per_h.values() for d in v]
            rec["change_diff"] = {
                "by_horizon":{h:{
                                  "n":             len(v),
                                  "median_signed_pt": _med(v),
                                  "median_abs_pt": _med([abs(x) for x in v]),
                                  "max_abs_pt":    _maxabs(v),
                                  "pass_median_abs": (
                                      _med([abs(x) for x in v]) is not None and
                                      _med([abs(x) for x in v]) <= T_CHANGE_MED
                                  ),
                               } for h,v in per_h.items()},
                "overall_max_abs_pt": _maxabs(all_diffs),
                "event_detail": ev_detail,
            }

        # 暫定判定（A: 価格水準絶対差 / B: 変化率差中央値 / C: 変化率差最大 / D: カバレッジ / E: サンプル数）
        ok_n = sum(1 for x in rec["old_event_coverage"].values() if x["ok"])
        pd = rec["price_diff"]; cd = rec["change_diff"]
        h_pass = (all(cd["by_horizon"][h].get("pass_median_abs") for h,_ in HORIZONS)
                  if cd.get("by_horizon") else None)
        max_ok = (cd.get("overall_max_abs_pt") is not None and
                  cd["overall_max_abs_pt"] <= T_CHANGE_MAX) if cd else None
        rec["verdict"] = {
            "coverage_ok_n":    ok_n,
            "coverage_total":   7,
            "coverage_rate":    round(ok_n/7,3),
            "pass_coverage":    ok_n == 7,
            "pass_price_diff":  pd.get("pass_median"),
            "pass_sample_n":    pd.get("pass_sample_n"),
            "pass_change_median": h_pass,
            "pass_change_max":    max_ok,
            # all_pass: A∧B∧C∧D∧E（いずれか None は False 扱い）
            "all_pass": all([
                ok_n == 7,
                pd.get("pass_median")   is True,
                pd.get("pass_sample_n") is True,
                h_pass                  is True,
                max_ok                  is True,
            ]),
        }

    except Exception as e:
        rec["error_type"] = type(e).__name__
        rec["error_msg"]  = safe_error(e)
        print(f"  [{source}:{symbol}] {type(e).__name__}: {safe_error(e)[:80]}")
    time.sleep(2)
    return rec


# -- FRED 基準データ取得
def get_fred_baseline():
    """FRED から価格水準比較用・変化率比較用データを取得する。"""
    if not FRED_API_KEY:
        print("[FRED] キー未設定 → FRED 比較をスキップ"); return None, None
    overlap_start = dt.date(2017,1,1)
    print(f"[FRED] 価格水準比較用取得 ({overlap_start} 〜 {PROBE_END}) ...")
    try:
        ov = fetch_fred("SP500", overlap_start, PROBE_END)
        print(f"  → {len(ov)} 件")
    except Exception as e:
        # 例外種別のみ表示。メッセージに URL/キーが含まれる可能性があるため safe_error を使用
        print(f"  → {type(e).__name__}: {safe_error(e)[:60]}"); ov = None
    reactions = {}
    for date_str, eid in RECENT_EVENTS:
        bd = dt.date.fromisoformat(date_str)
        try:
            data = fetch_fred("SP500", bd-dt.timedelta(days=10), bd+dt.timedelta(days=110))
            ch = calc_changes(data, bd)
            if ch: reactions[eid] = ch; print(f"  [FRED] {eid}: {ch}")
        except Exception as e:
            print(f"  [FRED] {eid} {type(e).__name__}: {safe_error(e)[:60]}")
        time.sleep(1)
    return ov, reactions


# -- メイン
def main():
    print(f"S&P500 ソース検証（調査専用・本番非影響）probe_end={PROBE_END}")
    ov, reactions = get_fred_baseline()
    records = []

    print("\n--- Stooq 候補 ---")
    for sym in STOOQ_CANDIDATES:
        print(f"\n[Stooq:{sym}]")
        records.append(probe_one("stooq", sym, ov or {}, reactions or {}))

    print("\n--- Yahoo 候補 ---")
    for sym in YAHOO_CANDIDATES:
        print(f"\n[Yahoo:{sym}]")
        records.append(probe_one("yahoo", sym, ov or {}, reactions or {}))

    # JSON レポート（生成失敗はワークフローを止める）
    report = {
        "generated_at":    dt.datetime.utcnow().isoformat()+"Z",
        "probe_end":       PROBE_END.isoformat(),
        "purpose":         "KI-001 / T-004,T-005,T-007 調査。本番JSON・Pages 未更新。",
        "thresholds": {
            "price_diff_median_abs_pct": T_PRICE_MED,
            "min_price_sample_n":        MIN_PRICE_SAMPLE_N,
            "change_diff_median_pt":     T_CHANGE_MED,
            "change_diff_max_pt":        T_CHANGE_MAX,
            "note": "暫定基準。実装前に正式決定する。",
        },
        "fred_key_present": bool(FRED_API_KEY),
        "fred_baseline_ok": ov is not None,
        "candidates": records,
    }
    json_path = os.path.join(OUT_DIR, "sp500_probe_report.json")
    try:
        with open(json_path,"w",encoding="utf-8") as f: json.dump(report,f,ensure_ascii=False,indent=2)
        print(f"\nJSON: {json_path}")
    except Exception as e:
        print(f"[FATAL] JSON 生成失敗: {e}", file=sys.stderr); sys.exit(1)

    # CSV（summary + coverage。生成失敗はワークフローを止める）
    try:
        sum_path = os.path.join(OUT_DIR,"sp500_probe_summary.csv")
        cov_path = os.path.join(OUT_DIR,"sp500_probe_coverage.csv")
        with open(sum_path,"w",newline="",encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["source","symbol","success","error_type","http_status","blocked",
                        "data_start","data_end","data_n","old_ok_n","coverage_rate",
                        "price_median_abs_pct","price_median_signed_pct","price_max_abs_pct",
                        "price_sample_n","pass_sample_n","price_pass",
                        "chg_d1_abs_med","chg_d7_abs_med","chg_d30_abs_med","chg_d90_abs_med",
                        "chg_max_abs","all_pass"])
            for r in records:
                vd=r.get("verdict",{}); pd=r.get("price_diff",{}); cd=r.get("change_diff",{})
                bh=cd.get("by_horizon",{})
                w.writerow([
                    r["source"],r["symbol"],r["success"],r.get("error_type",""),
                    r.get("http_status",""),r.get("blocked",""),
                    r.get("data_start",""),r.get("data_end",""),r.get("data_n",0),
                    vd.get("coverage_ok_n",""),vd.get("coverage_rate",""),
                    pd.get("median_abs_pct",""),pd.get("median_signed_pct",""),pd.get("max_abs_pct",""),
                    pd.get("sample_n",""),pd.get("pass_sample_n",""),vd.get("pass_price_diff",""),
                    bh.get("d1",{}).get("median_abs_pt",""),bh.get("d7",{}).get("median_abs_pt",""),
                    bh.get("d30",{}).get("median_abs_pt",""),bh.get("d90",{}).get("median_abs_pt",""),
                    cd.get("overall_max_abs_pt",""),vd.get("all_pass",""),
                ])
        with open(cov_path,"w",newline="",encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["source","symbol","event_id","target_date","used_date","value","ok"])
            for r in records:
                for eid,cv in r.get("old_event_coverage",{}).items():
                    w.writerow([r["source"],r["symbol"],eid,cv["target_date"],
                                 cv.get("used_date",""),cv.get("value",""),cv["ok"]])
        print(f"CSV: {sum_path}\nCSV: {cov_path}")
    except Exception as e:
        print(f"[FATAL] CSV 生成失敗: {e}", file=sys.stderr); sys.exit(1)

    print("\n完了。probe_out/ を確認してください。")

if __name__ == "__main__":
    main()
