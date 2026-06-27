"""
weekly_report_db.py — weekly_reports テーブル操作・draft 保存前検証

WeeklyDB を拡張し、weekly_reports 専用の操作を提供する。
スナップショット処理（weekly_asset_snapshots）は WeeklyDB が担う。

役割:
  - validate_draft(): ローカル draft の完全検証（DB接続不要）
  - build_db_payload(): DB INSERT 用 payload 構築（hash は NULL）
  - are_drafts_equal(): 既存 DB 行との冪等性比較
  - verify_saved_report(): INSERT 後の整合確認
  - WeeklyReportDB: get_report_full / delete_report（テスト用）を追加
"""
from __future__ import annotations

import datetime
import json
import re
from typing import Any

from weekly_dates import week_id_to_period
from weekly_db import WeeklyDB, WeeklyDBError
from weekly_report_builder import (
    compute_hash,
    check_restricted_leak,
    check_free_teaser_leak,
    validate_free_teaser,
    validate_paid_body,
)


class DraftValidationError(RuntimeError):
    """ローカル draft の検証エラー（秘密情報を含まない）。"""


# ─── WeeklyReportDB ──────────────────────────────────────────────────────────

class WeeklyReportDB(WeeklyDB):
    """weekly_reports テーブル専用操作。WeeklyDB を拡張。"""

    _FULL_SELECT = (
        "week_id,title,period_start,period_end,status,"
        "free_teaser,paid_body,teaser_hash,paid_body_hash,"
        "revision,generated_at,reviewed_at,reviewed_by,"
        "published_at,withdrawn_at,withdrawal_reason,"
        "created_at,updated_at"
    )

    def get_report_full(self, week_id: str) -> dict | None:
        """指定週のレポートを全カラムで返す（なければ None）。"""
        _, rows = self._request(
            "GET",
            f"/weekly_reports?week_id=eq.{week_id}&select={self._FULL_SELECT}",
        )
        return rows[0] if rows else None

    def delete_report(self, week_id: str) -> None:
        """指定週のレポートを削除する（テスト・クリーンアップ専用）。"""
        self._request(
            "DELETE",
            f"/weekly_reports?week_id=eq.{week_id}",
        )

    def patch_report_to_published(
        self,
        week_id: str,
        revision: int,
        payload: dict,
        *,
        allow_production: bool = False,
    ) -> list[dict]:
        """
        weekly_reports を draft → published に PATCH する。

        条件: week_id=eq.{week_id}&status=eq.draft&revision=eq.{revision}
        Prefer: return=representation で更新後の行を返す。

        Returns:
            更新された行のリスト（正常時は 1 件）
        """
        self._check_production_guard(allow_production)
        _, rows = self._request(
            "PATCH",
            (
                f"/weekly_reports"
                f"?week_id=eq.{week_id}&status=eq.draft&revision=eq.{revision}"
            ),
            body=payload,
            extra_headers={"Prefer": "return=representation"},
        )
        return rows if isinstance(rows, list) else ([rows] if rows else [])


# ─── draft 検証 ──────────────────────────────────────────────────────────────

def validate_draft(draft: dict, week_id: str) -> list[str]:
    """
    ローカル draft を完全検証する。DB 接続不要。

    Returns:
        errors: エラーメッセージのリスト（空 = OK）

    検証項目:
      1. week_id 一致
      2. revision >= 1
      3. generated_at 有効 ISO 日時
      4. free_teaser 存在
      5. paid_body 存在
      6. teaser_hash 存在・形式
      7. paid_body_hash 存在・形式
      8. hard_errors 空
      9. warnings がリスト
      10. free_teaser JSON Schema
      11. paid_body JSON Schema
      12. hash 再計算
      13. restricted leak check
      14. free_teaser paid 情報混入チェック
      15. period 整合
    """
    errors: list[str] = []

    # 1. week_id 一致
    if draft.get("week_id") != week_id:
        errors.append(
            f"[HARD] week_id 不一致: draft={draft.get('week_id')!r} / CLI={week_id!r}"
        )

    # 2. revision >= 1
    rev = draft.get("revision")
    if not isinstance(rev, int) or rev < 1:
        errors.append(f"[HARD] revision が無効: {rev!r}（1以上の整数が必要）")

    # 3. generated_at 有効 ISO 日時
    ga = draft.get("generated_at")
    if not ga or not isinstance(ga, str):
        errors.append(f"[HARD] generated_at が存在しないか文字列でない: {ga!r}")
    else:
        try:
            _parse_dt(ga)
        except ValueError:
            errors.append(f"[HARD] generated_at が ISO 日時として不正: {ga!r}")

    # 4-5. free_teaser / paid_body 存在
    ft  = draft.get("free_teaser")
    pb  = draft.get("paid_body")
    if not isinstance(ft, dict):
        errors.append("[HARD] free_teaser が存在しないか dict でない")
        ft = None
    if not isinstance(pb, dict):
        errors.append("[HARD] paid_body が存在しないか dict でない")
        pb = None

    # 6-7. hash 存在・形式
    _hex64 = re.compile(r"^[0-9a-f]{64}$")
    th  = draft.get("teaser_hash")
    pbh = draft.get("paid_body_hash")
    if not isinstance(th, str) or not _hex64.match(th):
        errors.append(f"[HARD] teaser_hash が無効（lowercase hex 64文字が必要）: {th!r}")
        th = None
    if not isinstance(pbh, str) or not _hex64.match(pbh):
        errors.append(f"[HARD] paid_body_hash が無効（lowercase hex 64文字が必要）: {pbh!r}")
        pbh = None

    # 8. hard_errors 空
    hard_errs = draft.get("hard_errors")
    if hard_errs:
        errors.append(f"[HARD] draft に hard_errors があります: {hard_errs}")

    # 9. warnings がリスト
    warns = draft.get("warnings")
    if warns is not None and not isinstance(warns, list):
        errors.append(f"[HARD] warnings がリストでない: {type(warns)}")

    # 基本整合エラーがあれば以後スキップ
    if errors:
        return errors

    # 10. free_teaser JSON Schema
    ft_errs = validate_free_teaser(ft)
    errors.extend(ft_errs)

    # 11. paid_body JSON Schema
    pb_errs = validate_paid_body(pb)
    errors.extend(pb_errs)

    # 12. hash 再計算
    if th is not None:
        recomputed = compute_hash(ft)
        if recomputed != th:
            errors.append(
                f"[HARD] teaser_hash 不一致: 再計算={recomputed[:16]}... "
                f"/ draft={th[:16]}..."
            )
    if pbh is not None:
        recomputed = compute_hash(pb)
        if recomputed != pbh:
            errors.append(
                f"[HARD] paid_body_hash 不一致: 再計算={recomputed[:16]}... "
                f"/ draft={pbh[:16]}..."
            )

    # 13. restricted leak check
    leak = check_restricted_leak({"free_teaser": ft, "paid_body": pb}, "")
    errors.extend(leak)

    # 14. free_teaser paid 情報混入チェック
    free_leak = check_free_teaser_leak(ft)
    errors.extend(free_leak)

    # 15. period 整合（week_id ↔ free_teaser.period_start/period_end）
    try:
        ps, pe = week_id_to_period(week_id)
        ft_ps = ft.get("period_start")
        ft_pe = ft.get("period_end")
        if str(ps) != ft_ps:
            errors.append(
                f"[HARD] period_start 不一致: week_id算出={ps} / free_teaser={ft_ps!r}"
            )
        if str(pe) != ft_pe:
            errors.append(
                f"[HARD] period_end 不一致: week_id算出={pe} / free_teaser={ft_pe!r}"
            )
    except ValueError as e:
        errors.append(f"[HARD] period 計算エラー: {e}")

    return errors


# ─── DB payload 構築 ─────────────────────────────────────────────────────────

def build_db_payload(draft: dict) -> dict:
    """
    ローカル draft から DB INSERT 用 payload を構築する。

    draft の teaser_hash / paid_body_hash は DB には保存しない（W2-4 で付与）。
    created_at / updated_at は DB デフォルトに任せるため送信しない。
    """
    ft = draft["free_teaser"]
    return {
        "week_id":          draft["week_id"],
        "title":            ft["title"],
        "period_start":     ft["period_start"],
        "period_end":       ft["period_end"],
        "status":           "draft",
        "free_teaser":      ft,
        "paid_body":        draft["paid_body"],
        "teaser_hash":      None,
        "paid_body_hash":   None,
        "revision":         draft["revision"],
        "generated_at":     draft["generated_at"],
        "reviewed_at":      None,
        "reviewed_by":      None,
        "published_at":     None,
        "withdrawn_at":     None,
        "withdrawal_reason": None,
    }


# ─── 冪等性比較 ──────────────────────────────────────────────────────────────

def are_drafts_equal(db_row: dict, payload: dict) -> bool:
    """
    DB 行と payload が意味的に同一かを比較する。

    比較対象:
      week_id, title, period_start, period_end, revision, generated_at,
      free_teaser（JSON canonical）, paid_body（JSON canonical）
    """
    for k in ("week_id", "title", "revision"):
        if db_row.get(k) != payload.get(k):
            return False

    # 日付文字列比較
    for k in ("period_start", "period_end"):
        if str(db_row.get(k, "")) != str(payload.get(k, "")):
            return False

    # generated_at: UTC 時刻として比較
    if not _datetimes_equal(db_row.get("generated_at"), payload.get("generated_at")):
        return False

    # JSONB 比較（canonical JSON）
    for k in ("free_teaser", "paid_body"):
        db_v  = db_row.get(k)
        pl_v  = payload.get(k)
        if _canonical(db_v) != _canonical(pl_v):
            return False

    return True


# ─── 保存後整合確認 ───────────────────────────────────────────────────────────

def verify_saved_report(db_row: dict, payload: dict) -> list[str]:
    """
    INSERT 後に再取得した DB 行を payload と照合する。

    Returns:
        mismatches: 不一致項目のリスト（空 = OK）
    """
    mismatches: list[str] = []

    # スカラー比較
    for k in ("week_id", "title", "revision"):
        if db_row.get(k) != payload.get(k):
            mismatches.append(f"{k}: DB={db_row.get(k)!r} / expected={payload.get(k)!r}")

    # 日付
    for k in ("period_start", "period_end"):
        if str(db_row.get(k, "")) != str(payload.get(k, "")):
            mismatches.append(
                f"{k}: DB={db_row.get(k)!r} / expected={payload.get(k)!r}"
            )

    # status
    if db_row.get("status") != "draft":
        mismatches.append(f"status: DB={db_row.get('status')!r} / expected='draft'")

    # generated_at
    if not _datetimes_equal(db_row.get("generated_at"), payload.get("generated_at")):
        mismatches.append(
            f"generated_at 不一致: DB={db_row.get('generated_at')!r} "
            f"/ expected={payload.get('generated_at')!r}"
        )

    # JSONB
    for k in ("free_teaser", "paid_body"):
        if _canonical(db_row.get(k)) != _canonical(payload.get(k)):
            mismatches.append(f"{k}: JSON 不一致")

    # NULL 列確認（draft 制約）
    null_cols = (
        "teaser_hash", "paid_body_hash",
        "reviewed_at", "reviewed_by",
        "published_at", "withdrawn_at", "withdrawal_reason",
    )
    for col in null_cols:
        if db_row.get(col) is not None:
            mismatches.append(f"{col}: DB={db_row.get(col)!r} / expected=NULL")

    return mismatches


# ─── 内部ユーティリティ ───────────────────────────────────────────────────────

def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_dt(s: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _datetimes_equal(a: str | None, b: str | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        utc = datetime.timezone.utc
        return _parse_dt(a).astimezone(utc) == _parse_dt(b).astimezone(utc)
    except (ValueError, TypeError):
        return False
