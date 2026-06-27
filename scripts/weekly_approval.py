"""
weekly_approval.py — Weekly Marketcast 承認ファイル管理

承認はローカルファイルへ記録する:
  ~/.local/share/marketcast-lab/approvals/YYYY-WXX_approval.json

DB の weekly_reports は status=draft のままとし、このモジュールはDBを変更しない。
承認ファイルには本文（free_teaser / paid_body）を含めない。hash のみ記録する。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from weekly_report_db import _canonical, _datetimes_equal

APPROVALS_DIR = Path.home() / ".local" / "share" / "marketcast-lab" / "approvals"
_APPROVAL_VERSION = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_WEEK_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")


class OperatorNameError(RuntimeError):
    """OPERATOR_NAME 環境変数が未設定または不正。"""


class ApprovalFileError(RuntimeError):
    """承認ファイルの読み書きエラー。"""


# ─── 承認者識別子 ─────────────────────────────────────────────────────────────

def get_operator_name() -> str:
    """
    OPERATOR_NAME 環境変数から承認者識別子を取得する。

    - 1〜64 文字の文字列
    - CLI フラグからは取得しない（セキュリティ境界）
    """
    name = os.environ.get("OPERATOR_NAME", "")
    if not name:
        raise OperatorNameError(
            "OPERATOR_NAME 環境変数が設定されていません。\n"
            "  export OPERATOR_NAME=your-name を実行してから再試行してください。"
        )
    if len(name) > 64:
        raise OperatorNameError(
            f"OPERATOR_NAME が長すぎます（{len(name)} 文字, 最大 64 文字）。"
        )
    return name


# ─── 承認ペイロード構築 ───────────────────────────────────────────────────────

def build_approval_payload(
    week_id: str,
    draft: dict,
    operator_name: str,
    now_utc: str,
) -> dict:
    """
    承認ファイル payload を構築する。

    free_teaser / paid_body の本文は含まない。hash のみ記録する。
    """
    return {
        "week_id":            week_id,
        "revision":           draft["revision"],
        "approved_at":        now_utc,
        "approved_by":        operator_name,
        "draft_generated_at": draft["generated_at"],
        "teaser_hash":        draft["teaser_hash"],
        "paid_body_hash":     draft["paid_body_hash"],
        "approval_version":   _APPROVAL_VERSION,
    }


# ─── スキーマ検証 ─────────────────────────────────────────────────────────────

def validate_approval_schema(approval: dict) -> list[str]:
    """
    承認ファイルの内容を検証する（外部ライブラリ不要）。

    Returns:
        errors: エラーリスト（空 = OK）
    """
    errors: list[str] = []

    required = {
        "week_id", "revision", "approved_at", "approved_by",
        "draft_generated_at", "teaser_hash", "paid_body_hash", "approval_version",
    }
    extra = set(approval.keys()) - required
    if extra:
        errors.append(f"[HARD] 不明なフィールド: {sorted(extra)}")

    missing = required - set(approval.keys())
    if missing:
        errors.append(f"[HARD] 必須フィールドなし: {sorted(missing)}")
        return errors

    if not isinstance(approval["week_id"], str) or not _WEEK_RE.match(approval["week_id"]):
        errors.append(f"[HARD] week_id が不正: {approval['week_id']!r}")

    rev = approval["revision"]
    if not isinstance(rev, int) or rev < 1:
        errors.append(f"[HARD] revision が不正: {rev!r}（1以上の整数が必要）")

    for k in ("approved_at", "draft_generated_at"):
        v = approval[k]
        if not isinstance(v, str):
            errors.append(f"[HARD] {k} が文字列でない: {v!r}")
        else:
            try:
                datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"[HARD] {k} が ISO 日時として不正: {v!r}")

    ab = approval["approved_by"]
    if not isinstance(ab, str) or len(ab) < 1 or len(ab) > 64:
        errors.append(f"[HARD] approved_by が不正（1〜64 文字）: {ab!r}")

    for k in ("teaser_hash", "paid_body_hash"):
        v = approval[k]
        if not isinstance(v, str) or not _HEX64.match(v):
            errors.append(f"[HARD] {k} が不正（小文字 hex 64 文字）: {str(v)[:20]!r}")

    if approval["approval_version"] != _APPROVAL_VERSION:
        errors.append(
            f"[HARD] approval_version が不正: {approval['approval_version']!r}"
            f"（期待値: {_APPROVAL_VERSION}）"
        )

    return errors


# ─── 冪等性比較 ──────────────────────────────────────────────────────────────

def are_approvals_equal(a: dict, b: dict) -> bool:
    """
    2 つの承認ファイルが同一内容かを比較する（冪等性チェック）。

    approved_at は比較しない（既存の承認時刻を維持するため）。
    """
    for k in ("week_id", "revision", "approved_by", "teaser_hash", "paid_body_hash"):
        if a.get(k) != b.get(k):
            return False
    if not _datetimes_equal(a.get("draft_generated_at"), b.get("draft_generated_at")):
        return False
    return True


# ─── ファイル I/O ─────────────────────────────────────────────────────────────

def _approval_path(week_id: str, path: Path | None) -> Path:
    if path is not None:
        return path
    return APPROVALS_DIR / f"{week_id}_approval.json"


def load_approval(week_id: str, path: Path | None = None) -> dict | None:
    """
    承認ファイルを読み込む。ファイルが存在しない場合は None を返す。
    """
    p = _approval_path(week_id, path)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ApprovalFileError(f"承認ファイルの読み込みに失敗しました: {p}: {e}") from e


def save_approval(approval: dict, path: Path | None = None) -> Path:
    """
    承認ファイルを保存する。

    ディレクトリ: 700 / ファイル: 600（アトミック書き込み）
    """
    week_id = approval.get("week_id", "unknown")
    p = _approval_path(week_id, path)

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.parent.chmod(stat.S_IRWXU)
        except OSError:
            pass

        content = json.dumps(approval, ensure_ascii=False, indent=2) + "\n"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
        tmp.rename(p)
    except OSError as e:
        raise ApprovalFileError(f"承認ファイルの保存に失敗しました: {p}: {e}") from e

    return p


# ─── DB draft 照合 ────────────────────────────────────────────────────────────

def verify_db_draft_matches_local(db_row: dict, draft: dict) -> list[str]:
    """
    DB の draft 行とローカル draft が一致することを確認する。

    承認フロー開始前に、DB の内容がローカルと同期していることを保証する。
    DB は変更しない。

    Returns:
        errors: 不一致・不正のリスト（空 = OK）
    """
    from weekly_report_builder import compute_hash

    errors: list[str] = []

    status = db_row.get("status")
    if status == "withdrawn":
        errors.append("[HARD] status=withdrawn: 承認・公開不可")
        return errors
    if status == "published":
        errors.append("[HARD] status=published: 既に公開済みです")
        return errors
    if status != "draft":
        errors.append(f"[HARD] 予期しない status: {status!r}")
        return errors

    if db_row.get("week_id") != draft.get("week_id"):
        errors.append(
            f"[HARD] week_id 不一致: DB={db_row.get('week_id')!r} / local={draft.get('week_id')!r}"
        )

    if db_row.get("revision") != draft.get("revision"):
        errors.append(
            f"[HARD] revision 不一致: DB={db_row.get('revision')!r} / local={draft.get('revision')!r}"
        )

    if not _datetimes_equal(db_row.get("generated_at"), draft.get("generated_at")):
        errors.append(
            f"[HARD] generated_at 不一致: "
            f"DB={db_row.get('generated_at')!r} / local={draft.get('generated_at')!r}"
        )

    if _canonical(db_row.get("free_teaser")) != _canonical(draft.get("free_teaser")):
        errors.append("[HARD] free_teaser が DB とローカルで異なります")

    if _canonical(db_row.get("paid_body")) != _canonical(draft.get("paid_body")):
        errors.append("[HARD] paid_body が DB とローカルで異なります")

    if db_row.get("teaser_hash") is not None:
        errors.append(
            f"[HARD] DB の teaser_hash が NULL でない: {db_row.get('teaser_hash')!r}"
        )
    if db_row.get("paid_body_hash") is not None:
        errors.append(
            f"[HARD] DB の paid_body_hash が NULL でない: {db_row.get('paid_body_hash')!r}"
        )

    local_th = draft.get("teaser_hash")
    local_pbh = draft.get("paid_body_hash")
    if local_th and draft.get("free_teaser"):
        recomputed = compute_hash(draft["free_teaser"])
        if recomputed != local_th:
            errors.append("[HARD] ローカル draft の teaser_hash が free_teaser と一致しません")
    if local_pbh and draft.get("paid_body"):
        recomputed = compute_hash(draft["paid_body"])
        if recomputed != local_pbh:
            errors.append("[HARD] ローカル draft の paid_body_hash が paid_body と一致しません")

    return errors


# ─── 承認入力パース ───────────────────────────────────────────────────────────

def parse_approval_input(user_input: str, week_id: str) -> bool:
    """
    承認入力を検証する。

    期待される入力: "APPROVE YYYY-WXX"（完全一致）

    Returns:
        True = 承認確認 / False = 不一致（キャンセル扱い）
    """
    expected = f"APPROVE {week_id}"
    return user_input.strip() == expected
