"""
Weekly Marketcast — 資産定義・閾値定数

変更するとDBのrestrictedカラムと不整合になる可能性があるため、
RESTRICTED_ASSETS と ASSET_CONFIGS の restricted フィールドは
supabase/migrations の CHECK 制約と一致させること。
"""
from __future__ import annotations

RESTRICTED_ASSETS: frozenset[str] = frozenset({"gold", "sp500"})

# 6資産の固定定義
ASSET_CONFIGS: list[dict] = [
    {
        "asset_key":     "wti",
        "source":        "fred",
        "fred_id":       "DCOILWTICO",
        "label":         "WTI原油先物",
        "restricted":    False,
        "change_type":   "pct",
        "flat_low":      -1.0,
        "flat_high":     1.0,
    },
    {
        "asset_key":     "gold",
        "source":        "stooq",
        "stooq_id":      "gld.us",
        "yahoo_symbol":  "GLD",
        "label":         "金(GLD)",
        "restricted":    True,
        "change_type":   "pct",
        "flat_low":      -0.5,
        "flat_high":     0.5,
    },
    {
        "asset_key":     "sp500",
        "source":        "fred",
        "fred_id":       "SP500",
        "label":         "S&P500",
        "restricted":    True,
        "change_type":   "pct",
        "flat_low":      -1.0,
        "flat_high":     1.0,
    },
    {
        "asset_key":     "ust10y",
        "source":        "fred",
        "fred_id":       "DGS10",
        "label":         "米10年債利回り",
        "restricted":    False,
        "change_type":   "pt",
        "flat_low":      -0.05,
        "flat_high":     0.05,
    },
    {
        "asset_key":     "usdjpy",
        "source":        "fred",
        "fred_id":       "DEXJPUS",
        "label":         "ドル円",
        "restricted":    False,
        "change_type":   "pct",
        "flat_low":      -0.5,
        "flat_high":     0.5,
    },
    {
        "asset_key":     "vix",
        "source":        "fred",
        "fred_id":       "VIXCLS",
        "label":         "VIX(恐怖指数)",
        "restricted":    False,
        "change_type":   "pct",
        "flat_low":      -5.0,
        "flat_high":     5.0,
    },
]

ASSET_KEYS: list[str] = [c["asset_key"] for c in ASSET_CONFIGS]
ASSET_CONFIG_MAP: dict[str, dict] = {c["asset_key"]: c for c in ASSET_CONFIGS}

# 本番 Supabase project ref（このrefを含むURLへのテスト書き込みを禁止）
PROD_PROJECT_REF = "lvsustmfqrxjnfgdtlna"


def get_asset_config(asset_key: str) -> dict:
    cfg = ASSET_CONFIG_MAP.get(asset_key)
    if cfg is None:
        raise ValueError(f"Unknown asset_key: {asset_key!r}")
    return cfg


def is_restricted(asset_key: str) -> bool:
    """restricted は外部入力ではなく、この定数から決定する。"""
    return asset_key in RESTRICTED_ASSETS


def is_production_url(url: str) -> bool:
    return PROD_PROJECT_REF in url
