"""
Weekly Marketcast — スナップショット構築・HARD/WARN 判定

取得済み観測値からスナップレコードを構築し、
HARD / WARN 条件を評価する。

restricted 生値をログへ出さない。差分計算出力にも含めない。
"""
from __future__ import annotations

import datetime
from typing import Any

from weekly_config import ASSET_CONFIGS, ASSET_CONFIG_MAP, is_restricted


# ── スナップレコード構築 ──────────────────────────────────────────────

def build_snapshot_record(
    asset_config: dict,
    observation: tuple[str, float] | None,
    source_used: str,
    snapshot_taken_at: str,
    week_id: str,
    seeded: bool = False,
    seed_source: str | None = None,
) -> dict[str, Any]:
    """
    1資産分のスナップレコードを返す。
    value は restricted 資産でも DB 保存用として含める
    （ログ・画面へは出さないこと）。
    """
    asset_key = asset_config["asset_key"]
    restricted = is_restricted(asset_key)  # 外部入力に依存しない

    if observation is None:
        return {
            "week_id":            week_id,
            "asset_key":          asset_key,
            "source":             source_used,
            "value":              None,
            "as_of":              None,
            "status":             "no_data",
            "restricted":         restricted,
            "seeded":             seeded,
            "seed_source":        seed_source,
            "snapshot_taken_at":  snapshot_taken_at,
        }

    date_str, value = observation
    return {
        "week_id":            week_id,
        "asset_key":          asset_key,
        "source":             source_used,
        "value":              value,
        "as_of":              date_str,
        "status":             "ok",
        "restricted":         restricted,
        "seeded":             seeded,
        "seed_source":        seed_source,
        "snapshot_taken_at":  snapshot_taken_at,
    }


def build_all_records(
    fetch_results: dict[str, tuple[tuple[str, float] | None, str]],
    snapshot_taken_at: str,
    week_id: str,
    seeded: bool = False,
    seed_source: str | None = None,
) -> list[dict[str, Any]]:
    """
    fetch_results: {asset_key: (observation_or_None, source_used)}
    ASSET_CONFIGS の順序で 6 件のレコードリストを返す。
    """
    records = []
    for cfg in ASSET_CONFIGS:
        key = cfg["asset_key"]
        obs, src = fetch_results.get(key, (None, cfg.get("fred_id", cfg.get("stooq_id", ""))))
        records.append(build_snapshot_record(cfg, obs, src, snapshot_taken_at, week_id, seeded, seed_source))
    return records


# ── HARD / WARN 判定 ─────────────────────────────────────────────────

def check_snapshots(
    records: list[dict[str, Any]],
    period_start: datetime.date,
    period_end: datetime.date,
) -> tuple[list[str], list[str]]:
    """
    Returns: (hard_errors, warnings)
    呼び出し元はhardがある場合DBへ書き込まないこと。
    """
    hard: list[str] = []
    warn: list[str] = []

    if len(records) != 6:
        hard.append(f"スナップレコード件数が6件でない: {len(records)}件")
        return hard, warn

    # status チェック
    non_ok = [r for r in records if r.get("status") != "ok"]
    if len(non_ok) >= 2:
        hard.append(f"status!=ok が2件以上: {[r['asset_key'] for r in non_ok]}")
    elif len(non_ok) == 1:
        warn.append(f"status!=ok が1件: {non_ok[0]['asset_key']} ({non_ok[0]['status']})")

    # as_of 週外チェック
    outside: list[str] = []
    for r in records:
        if r.get("as_of") is None:
            continue
        as_of = datetime.date.fromisoformat(r["as_of"])
        if as_of < period_start or as_of > period_end:
            outside.append(r["asset_key"])
    if len(outside) >= 2:
        hard.append(f"as_of が対象週外の資産が2件以上: {outside}")
    elif len(outside) == 1:
        warn.append(f"as_of が対象週外: {outside[0]}")

    # as_of が period_end より古い（週内だが最終営業日でない）
    for r in records:
        if r.get("as_of") is None:
            continue
        as_of = datetime.date.fromisoformat(r["as_of"])
        if period_start <= as_of < period_end:
            warn.append(
                f"{r['asset_key']}: as_of={r['as_of']} が period_end({period_end}) より古い"
            )

    # restricted 整合確認
    for r in records:
        expected = is_restricted(r["asset_key"])
        if r.get("restricted") != expected:
            hard.append(
                f"restricted 不一致: {r['asset_key']} "
                f"(期待={expected}, 実際={r.get('restricted')})"
            )

    # Yahoo フォールバック使用
    for r in records:
        if r.get("source", "").startswith("yahoo_"):
            warn.append(f"{r['asset_key']}: Yahoo フォールバック使用 (source={r['source']})")

    # seeded スナップ
    for r in records:
        if r.get("seeded"):
            warn.append(f"{r['asset_key']}: seeded スナップ")

    return hard, warn


# ── プレビュー表示（restricted 生値を出さない）───────────────────────

def format_preview_row(r: dict[str, Any]) -> str:
    """1レコードのプレビュー行を返す（value は表示しない）。"""
    status   = r.get("status", "?")
    as_of    = r.get("as_of") or "—"
    src      = r.get("source", "?")
    restr    = "restricted" if r.get("restricted") else "—"
    seeded   = " [seeded]" if r.get("seeded") else ""
    return (
        f"  {r['asset_key']:<8}  status={status:<8}  as_of={as_of:<12}"
        f"  {restr:<10}  source={src}{seeded}"
    )


def print_preview(
    records: list[dict[str, Any]],
    hard: list[str],
    warn: list[str],
    week_id: str,
) -> None:
    """DB書き込み前プレビューを表示する。"""
    print(f"\n{'─'*60}")
    print(f"【プレビュー】 {week_id}")
    print(f"{'─'*60}")
    for r in records:
        print(format_preview_row(r))
    if warn:
        print("\n[WARN]")
        for w in warn:
            print(f"  ⚠  {w}")
    if hard:
        print("\n[HARD]")
        for h in hard:
            print(f"  ✖  {h}")
    print(f"{'─'*60}\n")
