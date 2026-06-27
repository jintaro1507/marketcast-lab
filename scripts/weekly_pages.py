"""
weekly_pages.py — 公開 JSON 構築・Schema 検証・index 更新・ファイル書き込み・deployed JSON 検証

公開ワークフロー (W2-4B) のコアライブラリ。CLI は含まない。

制約:
  - git commit/push を実行しない
  - paid_body / restricted 生値を公開 JSON に含めない
  - Pages ファイル（data/weekly/）は project root 相対で管理
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema

# ─── 定数 ─────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).parent.parent

PUBLICATIONS_DIR = Path.home() / ".local" / "share" / "marketcast-lab" / "publications"
ARCHIVES_DIR     = Path.home() / ".local" / "share" / "marketcast-lab" / "archives"

PUBLICATION_VERSION = 1
SCHEMA_VERSION      = 1
PAGES_BASE_URL      = "https://marketcast.oneshorejp.com"

PUBLIC_TEASER_DIR = _PROJECT_ROOT / "data" / "weekly"
INDEX_PATH        = PUBLIC_TEASER_DIR / "index.json"

_SCHEMA_PUBLIC_TEASER = _PROJECT_ROOT / "schemas" / "weekly_public_teaser.schema.json"
_SCHEMA_PUBLIC_INDEX  = _PROJECT_ROOT / "schemas" / "weekly_public_index.schema.json"

_STAGE_TRANSITIONS: dict[str, str] = {
    "prepared":      "pages_verified",
    "pages_verified": "db_published",
    "db_published":  "completed",
}

_VERIFY_RETRIES  = 5
_VERIFY_INTERVAL = 15

# 公開 JSON に含めてはいけないキー
PUBLIC_FORBIDDEN_KEYS: frozenset[str] = frozenset({
    "paid_body",
    "paid_body_hash",
    "reviewed_by",
    "approved_by",
    "approval",
    "warnings",
    "hard_errors",
    "score",
    "matched_axes",
    "unmatched_axes",
    "timelines",
    "why_reaction",
    "key_insight",
    "value",
    "current_value",
    "previous_value",
    "latest_value",
    "raw_value",
    "price",
    "close",
    "api_key",
    "service_role_key",
    "authorization",
    "jwt",
})


# ─── 例外 ──────────────────────────────────────────────────────────────────────

class PubStateError(RuntimeError):
    """公開ステート管理エラー。"""


class PublicFileError(RuntimeError):
    """公開ファイル操作エラー。"""


class IndexUpdateError(RuntimeError):
    """インデックス更新エラー（week_id 競合など）。"""


# ─── 内部ユーティリティ ────────────────────────────────────────────────────────

def _week_id_key(week_id: str) -> tuple[int, int]:
    """week_id を数値比較キーに変換する（'2026-W09' < '2026-W10' を保証）。"""
    m = re.match(r"^(\d{4})-W(\d{1,2})$", week_id)
    if not m:
        raise ValueError(f"Invalid week_id: {week_id!r}")
    return int(m.group(1)), int(m.group(2))


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """write → fsync → rename によるアトミック書き込み。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _now_utc_seconds() -> str:
    """秒単位に正規化した現在 UTC 時刻（ISO 8601）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sleep(seconds: float) -> None:
    """テストでモック可能な sleep。"""
    time.sleep(seconds)


def _to_json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


# ─── 公開ステート管理 ──────────────────────────────────────────────────────────

def _pub_state_path(week_id: str, base_dir: Path | None = None) -> Path:
    d = base_dir if base_dir is not None else PUBLICATIONS_DIR
    return d / f"{week_id}_publication.json"


def load_pub_state(week_id: str, path: Path | None = None) -> dict | None:
    """公開ステートを読み込む。存在しない場合は None を返す。"""
    p = path if path is not None else _pub_state_path(week_id)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise PubStateError(f"公開ステートの読み込みに失敗しました: {e}") from e


def save_pub_state(state: dict, path: Path | None = None) -> Path:
    """公開ステートを保存する（dir 700 / file 600 / atomic write）。"""
    week_id = state.get("week_id", "unknown")
    p = path if path is not None else _pub_state_path(week_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    _atomic_write(p, _to_json(state).encode("utf-8"), mode=0o600)
    return p


def build_initial_pub_state(
    week_id: str,
    approval: dict,
    published_at: str,
) -> dict:
    """
    初期公開ステートを構築する。

    published_at は prepare 時に決定し、finalize 時に再利用する（再生成しない）。
    """
    return {
        "publication_version": PUBLICATION_VERSION,
        "week_id": week_id,
        "revision": approval["revision"],
        "teaser_hash": approval["teaser_hash"],
        "paid_body_hash": approval["paid_body_hash"],
        "published_at": published_at,
        "stage": "prepared",
        "prepared_at": _now_utc_seconds(),
    }


def advance_stage(state: dict, new_stage: str) -> dict:
    """
    ステージを進める（不正な遷移は PubStateError）。

    Returns:
        新しいステートオブジェクト（元を変更しない）
    """
    current = state.get("stage")
    expected = _STAGE_TRANSITIONS.get(current)  # type: ignore[arg-type]
    if expected != new_stage:
        raise PubStateError(
            f"ステージ遷移が不正です: {current!r} → {new_stage!r} (expected: {expected!r})"
        )
    new_state = dict(state)
    new_state["stage"] = new_stage
    new_state[f"{new_stage}_at"] = _now_utc_seconds()
    return new_state


# ─── 公開 JSON 構築 ────────────────────────────────────────────────────────────

def build_public_teaser(draft: dict, approval: dict, pub_state: dict) -> dict:
    """承認済み draft から公開 teaser JSON を構築する。paid_body を含まない。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "week_id": draft["week_id"],
        "revision": draft["revision"],
        "published_at": pub_state["published_at"],
        "teaser_hash": approval["teaser_hash"],
        "free_teaser": draft["free_teaser"],
    }


def validate_public_teaser(teaser: dict) -> list[str]:
    """公開 teaser を Schema 検証・free_teaser 検証・禁止キー検証する。"""
    errors: list[str] = []

    try:
        with open(_SCHEMA_PUBLIC_TEASER, encoding="utf-8") as f:
            schema = json.load(f)
        jsonschema.validate(teaser, schema)
    except jsonschema.ValidationError as e:
        errors.append(f"[スキーマ] {e.message}")
    except FileNotFoundError:
        errors.append("[スキーマ] weekly_public_teaser.schema.json が見つかりません")

    ft = teaser.get("free_teaser")
    if isinstance(ft, dict):
        from weekly_report_builder import validate_free_teaser
        errors.extend(validate_free_teaser(ft))

    errors.extend(check_forbidden_keys_deep(teaser))
    return errors


# ─── 禁止キー検査 ──────────────────────────────────────────────────────────────

def check_forbidden_keys_deep(obj: Any, path: str = "") -> list[str]:
    """ネストされたオブジェクト/配列を再帰的に検索して禁止キーを検出する。"""
    errors: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            full_path = f"{path}.{k}" if path else k
            if k in PUBLIC_FORBIDDEN_KEYS:
                errors.append(
                    f"[FORBIDDEN] 公開 JSON に禁止キー {k!r} が含まれています (path: {full_path})"
                )
            else:
                errors.extend(check_forbidden_keys_deep(v, full_path))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            errors.extend(check_forbidden_keys_deep(v, f"{path}[{i}]"))
    return errors


# ─── インデックス管理 ──────────────────────────────────────────────────────────

def build_index_entry(teaser: dict) -> dict:
    """teaser から index エントリを構築する。"""
    ft = teaser.get("free_teaser", {})
    return {
        "week_id":      teaser["week_id"],
        "revision":     teaser["revision"],
        "published_at": teaser["published_at"],
        "title":        ft.get("title", ""),
        "period_start": ft.get("period_start", ""),
        "period_end":   ft.get("period_end", ""),
        "env_label":    ft.get("env_label", ""),
        "teaser_hash":  teaser["teaser_hash"],
    }


def load_public_index(path: Path) -> dict | None:
    """公開インデックスを読み込む。存在しない場合は None を返す。"""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise PublicFileError(f"インデックスの読み込みに失敗しました: {e}") from e


def _build_empty_index() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at":     _now_utc_seconds(),
        "latest_week_id": None,
        "reports":        [],
    }


def update_index(index: dict | None, new_entry: dict) -> tuple[dict, str]:
    """
    インデックスにエントリを追加する。

    Returns:
        (new_index, action): action は "added" または "unchanged"

    Raises:
        IndexUpdateError: 同一 week_id に異なる内容（teaser_hash 不一致）が存在する場合
    """
    if index is None:
        index = _build_empty_index()

    week_id = new_entry["week_id"]
    reports = list(index.get("reports", []))

    existing = next((r for r in reports if r["week_id"] == week_id), None)
    if existing is not None:
        if existing["teaser_hash"] == new_entry["teaser_hash"]:
            return index, "unchanged"
        raise IndexUpdateError(
            f"[HARD] {week_id} のインデックスに異なる teaser_hash が存在します。\n"
            f"  既存: {existing['teaser_hash'][:16]}...\n"
            f"  新規: {new_entry['teaser_hash'][:16]}..."
        )

    reports.append(new_entry)
    latest = max((r["week_id"] for r in reports), key=_week_id_key)
    reports.sort(key=lambda r: _week_id_key(r["week_id"]), reverse=True)

    new_index = dict(index)
    new_index["reports"]        = reports
    new_index["latest_week_id"] = latest
    new_index["updated_at"]     = _now_utc_seconds()
    return new_index, "added"


def validate_public_index(index: dict) -> list[str]:
    """公開インデックスを Schema 検証・禁止キー検証する。"""
    errors: list[str] = []
    try:
        with open(_SCHEMA_PUBLIC_INDEX, encoding="utf-8") as f:
            schema = json.load(f)
        jsonschema.validate(index, schema)
    except jsonschema.ValidationError as e:
        errors.append(f"[スキーマ] {e.message}")
    except FileNotFoundError:
        errors.append("[スキーマ] weekly_public_index.schema.json が見つかりません")
    errors.extend(check_forbidden_keys_deep(index))
    return errors


# ─── ファイル書き込み ──────────────────────────────────────────────────────────

def write_public_teaser(teaser: dict, path: Path) -> str:
    """
    公開 teaser をアトミック書き込みする。

    Returns:
        "written" または "unchanged"

    Raises:
        PublicFileError: 既存ファイルに異なる内容が存在する場合
    """
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing == teaser:
            return "unchanged"
        raise PublicFileError(
            f"[HARD] {path.name} に異なる内容の teaser が存在します。\n"
            f"  既存 teaser_hash: {existing.get('teaser_hash', '?')[:16]}...\n"
            f"  新規 teaser_hash: {teaser.get('teaser_hash', '?')[:16]}..."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, _to_json(teaser).encode("utf-8"), mode=0o644)
    return "written"


def write_public_index(index: dict, path: Path) -> None:
    """公開インデックスをアトミック書き込みする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, _to_json(index).encode("utf-8"), mode=0o644)


# ─── アーカイブ ────────────────────────────────────────────────────────────────

def archive_draft(draft_path: Path, week_id: str) -> Path:
    """
    ローカル draft ファイルをアーカイブする。

    アーカイブ先: ~/.local/share/marketcast-lab/archives/YYYY-WXX_draft.json
    dir 700 / file 600 / atomic write / idempotent（同一内容なら上書き可）
    """
    content = draft_path.read_bytes()
    archive_path = ARCHIVES_DIR / f"{week_id}_draft.json"
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(ARCHIVES_DIR, 0o700)
    _atomic_write(archive_path, content, mode=0o600)
    return archive_path


# ─── HTTP 検証 ────────────────────────────────────────────────────────────────

def _teaser_url(week_id: str, cache_bust: str) -> str:
    return f"{PAGES_BASE_URL}/data/weekly/{week_id}.json?v={cache_bust}"


def _index_url(cache_bust: str) -> str:
    return f"{PAGES_BASE_URL}/data/weekly/index.json?v={cache_bust}"


def _fetch_json(url: str) -> dict | None:
    """URL から JSON を取得する。失敗時は None を返す（例外を伝播しない）。"""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError):
        return None


def _compare_teaser(fetched: dict, expected: dict) -> list[str]:
    from weekly_report_builder import compute_hash
    from weekly_report_db import _datetimes_equal

    errors: list[str] = []
    if fetched.get("teaser_hash") != expected.get("teaser_hash"):
        errors.append(
            f"[MISMATCH] teaser_hash: "
            f"deployed={fetched.get('teaser_hash', '?')[:16]}... / "
            f"expected={expected.get('teaser_hash', '?')[:16]}..."
        )
    if fetched.get("week_id") != expected.get("week_id"):
        errors.append(
            f"[MISMATCH] week_id: deployed={fetched.get('week_id')!r} / expected={expected.get('week_id')!r}"
        )
    if fetched.get("revision") != expected.get("revision"):
        errors.append(
            f"[MISMATCH] revision: deployed={fetched.get('revision')!r} / expected={expected.get('revision')!r}"
        )
    if not _datetimes_equal(fetched.get("published_at"), expected.get("published_at")):
        errors.append(
            f"[MISMATCH] published_at: "
            f"deployed={fetched.get('published_at')!r} / expected={expected.get('published_at')!r}"
        )
    ft = fetched.get("free_teaser")
    if ft is not None:
        computed = compute_hash(ft)
        if computed != expected.get("teaser_hash"):
            errors.append(
                f"[MISMATCH] deployed free_teaser hash が teaser_hash と不一致: "
                f"computed={computed[:16]}..."
            )
    return errors


def _compare_index_entry(index: dict, week_id: str, expected_entry: dict) -> list[str]:
    errors: list[str] = []
    entry = next((r for r in index.get("reports", []) if r.get("week_id") == week_id), None)
    if entry is None:
        errors.append(f"[MISSING] インデックスに {week_id} エントリが存在しません")
        return errors
    if entry.get("teaser_hash") != expected_entry.get("teaser_hash"):
        errors.append(
            f"[MISMATCH] index entry teaser_hash: "
            f"deployed={entry.get('teaser_hash', '?')[:16]}... / "
            f"expected={expected_entry.get('teaser_hash', '?')[:16]}..."
        )
    if entry.get("revision") != expected_entry.get("revision"):
        errors.append(
            f"[MISMATCH] index entry revision: "
            f"deployed={entry.get('revision')!r} / expected={expected_entry.get('revision')!r}"
        )
    return errors


def verify_deployed_teaser(
    week_id: str,
    expected_teaser: dict,
    *,
    retries: int = _VERIFY_RETRIES,
    interval: int = _VERIFY_INTERVAL,
) -> list[str]:
    """
    デプロイ済み teaser を HTTP GET で検証する。

    cache bust: ?v=<teaser_hash[:12]>
    最大 retries 回、interval 秒間隔でリトライ。

    Returns:
        errors: 問題のリスト（空 = OK）
    """
    cache_bust = expected_teaser.get("teaser_hash", "")[:12]
    url = _teaser_url(week_id, cache_bust)
    last_errors: list[str] = [f"[HTTP] 未試行: {url}"]

    for attempt in range(retries):
        if attempt > 0:
            _sleep(interval)
        fetched = _fetch_json(url)
        if fetched is None:
            last_errors = [f"[HTTP] teaser 取得失敗 (attempt {attempt + 1}/{retries}): {url}"]
            continue
        errors = _compare_teaser(fetched, expected_teaser)
        if not errors:
            return []
        last_errors = errors

    return last_errors


def verify_deployed_index(
    week_id: str,
    expected_entry: dict,
    *,
    retries: int = _VERIFY_RETRIES,
    interval: int = _VERIFY_INTERVAL,
) -> list[str]:
    """
    デプロイ済みインデックスを HTTP GET で検証する。

    Returns:
        errors: 問題のリスト（空 = OK）
    """
    cache_bust = expected_entry.get("teaser_hash", "")[:12]
    url = _index_url(cache_bust)
    last_errors: list[str] = [f"[HTTP] 未試行: {url}"]

    for attempt in range(retries):
        if attempt > 0:
            _sleep(interval)
        fetched = _fetch_json(url)
        if fetched is None:
            last_errors = [f"[HTTP] index 取得失敗 (attempt {attempt + 1}/{retries}): {url}"]
            continue
        errors = _compare_index_entry(fetched, week_id, expected_entry)
        if not errors:
            return []
        last_errors = errors

    return last_errors
