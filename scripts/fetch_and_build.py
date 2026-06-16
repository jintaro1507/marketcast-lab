#!/usr/bin/env python3
"""
Marketcast Lab - データ取得＆加工プロトタイプ
================================================
公開情報（FRED経済データ + ニュースRSS）を取得し、
各資産クラスの「方向性・変化率」を計算して画面用JSONを出力する。

【重要な設計方針】
- 価格の「生データ」をそのまま再配布せず、変化率(%)など加工した値を出力する。
  FREDの一部系列(S&P500など)は再配布制限があるため、派生指標の表示に留める。
- FRED APIキーは環境変数から読む（コードに直接書かない＝GitHub Secretsで管理）。
- 取得失敗時もサイト全体が壊れないよう、各系列ごとに例外を握りつぶして記録する。

実行方法:
    export FRED_API_KEY=あなたのキー
    python3 fetch_and_build.py
"""

import os
import json
import datetime as dt
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# ------------------------------------------------------------------
# 追跡する系列の定義
#   id      : FREDの系列ID
#   label   : 画面表示名
#   asset   : 資産クラス（モックの色分けに対応：oil/gold/equity/crypto/fx/bond）
#   restricted : Trueなら再配布制限あり → 生値は出さず変化率のみ表示する
# ------------------------------------------------------------------
SERIES = [
    {"id": "DCOILWTICO",  "label": "WTI原油先物",   "asset": "oil",    "restricted": False},
    {"id": "DCOILBRENTEU","label": "Brent原油先物", "asset": "oil",    "restricted": False},
    {"id": "VIXCLS",      "label": "VIX(恐怖指数)", "asset": "equity", "restricted": False},
    {"id": "DGS10",       "label": "米10年債利回り","asset": "bond",   "restricted": False},
    {"id": "DEXJPUS",     "label": "ドル円",        "asset": "fx",     "restricted": False},
    {"id": "SP500",       "label": "S&P500",        "asset": "equity", "restricted": True},
    # 金は系列によって提供元の制限が異なるため、利用前に各系列のソース表記を要確認
    {"id": "GOLDPMGBD228NLBM", "label": "金(ロンドン値決め)", "asset": "gold", "restricted": True},
]

# 変化を測る期間（営業日ベースのおおよその値）
LOOKBACK_DAYS = 30


def fetch_series(series_id, days=120):
    """指定系列の直近観測値を取得して [(date, value), ...] を返す。"""
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY が設定されていません")

    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "observation_start": start,
        "sort_order": "asc",
    }
    url = f"{FRED_BASE}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "MarketcastLab/0.1"})
    with urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    out = []
    for obs in payload.get("observations", []):
        v = obs.get("value", ".")
        if v not in (".", "", None):  # FREDは欠損を "." で返す
            try:
                out.append((obs["date"], float(v)))
            except ValueError:
                pass
    return out


def compute_trend(observations):
    """
    観測値リストから、直近値・期間変化率・方向を計算する。
    生の系列そのものではなく「加工された要約」を返すのがポイント。
    """
    if len(observations) < 2:
        return None

    latest_date, latest_val = observations[-1]

    # LOOKBACK_DAYS 前に最も近い観測値を基準にする
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
    if pct > 0.3:
        direction = "up"
    elif pct < -0.3:
        direction = "down"
    else:
        direction = "flat"

    return {
        "as_of": latest_date,
        "base_date": base_date,
        "pct_change": round(pct, 1),
        "direction": direction,
    }


def build_payload():
    """全系列を処理して、画面用の最終JSON構造を組み立てる。"""
    results = []
    for s in SERIES:
        entry = {
            "label": s["label"],
            "asset": s["asset"],
            "status": "ok",
        }
        try:
            obs = fetch_series(s["id"])
            trend = compute_trend(obs)
            if trend is None:
                entry["status"] = "no_data"
            else:
                entry.update(trend)
                # 再配布制限のある系列は、生の最新値を含めず変化率だけ載せる
                if not s["restricted"]:
                    entry["latest_value"] = obs[-1][1]
                entry["restricted"] = s["restricted"]
        except (URLError, HTTPError, RuntimeError) as e:
            entry["status"] = "error"
            entry["error"] = str(e)
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
