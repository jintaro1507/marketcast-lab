"""
weekly_report_publish.py — Weekly Marketcast 公開遷移（内部ライブラリ）

このモジュールは published 遷移の内部関数を提供する。
一般公開 CLI ではなく、承認フロー後の DB 遷移ロジックを担う。

制約（このモジュールが行わないこと）:
  - Pages JSON 生成
  - git push
  - 本番 DB への published 更新（allow_production=False がデフォルト）
  - withdraw 操作
  - revision インクリメント

apply_published_transition() はローカル DB テスト専用。
"""
from __future__ import annotations

from weekly_report_builder import compute_hash
from weekly_report_db import _datetimes_equal


# ─── 公開前改変チェック ───────────────────────────────────────────────────────

def verify_pre_publish(
    approval: dict,
    draft: dict,
    db_row: dict,
) -> list[str]:
    """
    公開前に承認後の改変がないことを確認する。

    比較:
      - ローカル draft hash == 承認ファイル hash
      - DB コンテンツ hash == 承認ファイル hash
      - revision 一致（ローカル draft・DB・承認ファイル）
      - generated_at 一致（ローカル draft・DB・承認ファイル）

    Returns:
        errors: 問題のリスト（空 = OK）
    """
    errors: list[str] = []

    db_status = db_row.get("status")
    if db_status == "withdrawn":
        errors.append("[HARD] DB status=withdrawn: 公開不可")
        return errors
    if db_status != "draft":
        errors.append(f"[HARD] DB status が draft でない: {db_status!r}")
        return errors

    approval_th  = approval.get("teaser_hash", "")
    approval_pbh = approval.get("paid_body_hash", "")

    local_th  = draft.get("teaser_hash", "")
    local_pbh = draft.get("paid_body_hash", "")
    if local_th != approval_th:
        errors.append(
            f"[HARD] ローカル draft teaser_hash が承認ファイルと不一致\n"
            f"  local={local_th[:16]}... / approval={approval_th[:16]}..."
        )
    if local_pbh != approval_pbh:
        errors.append(
            f"[HARD] ローカル draft paid_body_hash が承認ファイルと不一致\n"
            f"  local={local_pbh[:16]}... / approval={approval_pbh[:16]}..."
        )

    if db_row.get("free_teaser") is not None:
        db_th = compute_hash(db_row["free_teaser"])
        if db_th != approval_th:
            errors.append(
                f"[HARD] DB free_teaser hash が承認ファイルと不一致\n"
                f"  db_computed={db_th[:16]}... / approval={approval_th[:16]}..."
            )

    if db_row.get("paid_body") is not None:
        db_pbh = compute_hash(db_row["paid_body"])
        if db_pbh != approval_pbh:
            errors.append(
                f"[HARD] DB paid_body hash が承認ファイルと不一致\n"
                f"  db_computed={db_pbh[:16]}... / approval={approval_pbh[:16]}..."
            )

    approval_rev = approval.get("revision")
    if draft.get("revision") != approval_rev:
        errors.append(
            f"[HARD] revision 不一致: draft={draft.get('revision')} / approval={approval_rev}"
        )
    if db_row.get("revision") != approval_rev:
        errors.append(
            f"[HARD] revision 不一致: DB={db_row.get('revision')} / approval={approval_rev}"
        )

    approval_gat = approval.get("draft_generated_at")
    if not _datetimes_equal(draft.get("generated_at"), approval_gat):
        errors.append(
            f"[HARD] generated_at 不一致: draft={draft.get('generated_at')!r} / approval={approval_gat!r}"
        )
    if not _datetimes_equal(db_row.get("generated_at"), approval_gat):
        errors.append(
            f"[HARD] generated_at 不一致: DB={db_row.get('generated_at')!r} / approval={approval_gat!r}"
        )

    return errors


# ─── published payload 構築 ───────────────────────────────────────────────────

def build_publish_payload(approval: dict, published_at: str) -> dict:
    """
    weekly_reports の published 遷移用 PATCH payload を構築する。

    week_id / title / period / free_teaser / paid_body / revision / generated_at は
    payload に含めない（PATCH で上書きしない）。
    """
    return {
        "status":            "published",
        "reviewed_at":       approval["approved_at"],
        "reviewed_by":       approval["approved_by"],
        "published_at":      published_at,
        "teaser_hash":       approval["teaser_hash"],
        "paid_body_hash":    approval["paid_body_hash"],
        "withdrawn_at":      None,
        "withdrawal_reason": None,
    }


# ─── 公開後整合確認 ───────────────────────────────────────────────────────────

def verify_published_report(
    db_row: dict,
    payload: dict,
    approval: dict,
) -> list[str]:
    """
    PATCH 後に再取得した DB 行を payload・承認ファイルと照合する。

    Returns:
        mismatches: 不一致項目のリスト（空 = OK）
    """
    mismatches: list[str] = []

    if db_row.get("status") != "published":
        mismatches.append(
            f"status: DB={db_row.get('status')!r} / expected='published'"
        )

    if not _datetimes_equal(db_row.get("reviewed_at"), payload.get("reviewed_at")):
        mismatches.append(
            f"reviewed_at: DB={db_row.get('reviewed_at')!r} / expected={payload.get('reviewed_at')!r}"
        )

    if db_row.get("reviewed_by") != payload.get("reviewed_by"):
        mismatches.append(
            f"reviewed_by: DB={db_row.get('reviewed_by')!r} / expected={payload.get('reviewed_by')!r}"
        )

    if not _datetimes_equal(db_row.get("published_at"), payload.get("published_at")):
        mismatches.append(
            f"published_at: DB={db_row.get('published_at')!r} / expected={payload.get('published_at')!r}"
        )

    if db_row.get("teaser_hash") != payload.get("teaser_hash"):
        mismatches.append(
            f"teaser_hash: DB={db_row.get('teaser_hash')!r} / expected={payload.get('teaser_hash')!r}"
        )

    if db_row.get("paid_body_hash") != payload.get("paid_body_hash"):
        mismatches.append(
            f"paid_body_hash: "
            f"DB={db_row.get('paid_body_hash')!r} / expected={payload.get('paid_body_hash')!r}"
        )

    if db_row.get("withdrawn_at") is not None:
        mismatches.append(
            f"withdrawn_at: DB={db_row.get('withdrawn_at')!r} / expected=NULL"
        )
    if db_row.get("withdrawal_reason") is not None:
        mismatches.append(
            f"withdrawal_reason: DB={db_row.get('withdrawal_reason')!r} / expected=NULL"
        )

    if db_row.get("free_teaser") is not None:
        computed_th = compute_hash(db_row["free_teaser"])
        if computed_th != approval.get("teaser_hash"):
            mismatches.append(
                f"free_teaser hash 不一致（改変検出）: "
                f"computed={computed_th[:16]}... / approval={approval.get('teaser_hash', '')[:16]}..."
            )

    if db_row.get("paid_body") is not None:
        computed_pbh = compute_hash(db_row["paid_body"])
        if computed_pbh != approval.get("paid_body_hash"):
            mismatches.append(
                f"paid_body hash 不一致（改変検出）: "
                f"computed={computed_pbh[:16]}... / approval={approval.get('paid_body_hash', '')[:16]}..."
            )

    if db_row.get("revision") != approval.get("revision"):
        mismatches.append(
            f"revision: DB={db_row.get('revision')!r} / approval={approval.get('revision')!r}"
        )

    return mismatches


# ─── 冪等性チェック ───────────────────────────────────────────────────────────

def are_published_idempotent(
    db_row: dict,
    payload: dict,
    approval: dict,
) -> bool:
    """
    既に published 行が存在する場合、同一内容かを確認する（冪等性チェック）。

    published_at は比較しない（既存の公開時刻を維持するため）。
    比較: reviewed_at, reviewed_by, teaser_hash, paid_body_hash, revision

    Returns:
        True = 同一（冪等成功） / False = 異なる（拒否）
    """
    if not _datetimes_equal(db_row.get("reviewed_at"), payload.get("reviewed_at")):
        return False
    if db_row.get("reviewed_by") != payload.get("reviewed_by"):
        return False
    if db_row.get("teaser_hash") != approval.get("teaser_hash"):
        return False
    if db_row.get("paid_body_hash") != approval.get("paid_body_hash"):
        return False
    if db_row.get("revision") != approval.get("revision"):
        return False
    return True


# ─── ローカル DB テスト専用: published 遷移実行 ──────────────────────────────

def apply_published_transition(
    db: "WeeklyReportDB",
    week_id: str,
    approval: dict,
    *,
    allow_production: bool = False,
) -> dict:
    """
    weekly_reports を draft → published に遷移させる（ローカル DB テスト専用）。

    Pages 反映確認・git push・本番 DB 更新は行わない。
    PATCH 条件: week_id=eq.{week_id}&status=eq.draft&revision=eq.{revision}

    Returns:
        更新後の DB 行（verify_published_report による整合確認済み）

    Raises:
        WeeklyDBError: PATCH 失敗・整合確認失敗
    """
    from datetime import datetime, timezone
    from weekly_db import WeeklyDBError

    published_at = datetime.now(timezone.utc).isoformat()
    payload = build_publish_payload(approval, published_at)
    revision = approval["revision"]

    rows = db.patch_report_to_published(
        week_id, revision, payload, allow_production=allow_production
    )

    if len(rows) == 0:
        raise WeeklyDBError(
            f"PATCH で更新された行が 0 件でした。\n"
            f"  week_id={week_id}, revision={revision} の draft 行が存在するか確認してください。"
        )
    if len(rows) > 1:
        raise WeeklyDBError(
            f"PATCH で複数行が更新されました（{len(rows)} 件）。予期しない状態です。"
        )

    db_row = rows[0]

    mismatches = verify_published_report(db_row, payload, approval)
    if mismatches:
        raise WeeklyDBError(
            "公開後整合確認に失敗しました:\n" + "\n".join(f"  {m}" for m in mismatches)
        )

    return db_row
