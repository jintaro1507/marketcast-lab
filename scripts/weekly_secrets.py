"""
Weekly Marketcast — 秘密情報管理

保存先: ~/.config/marketcast-lab/.env
ディレクトリ権限: 700 / ファイル権限: 600

読み込み後のキー値を:
  - ターミナルへ表示しない
  - ログへ出力しない
  - 例外メッセージに含めない
  - CLI 引数で受け取らない
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

_CONFIG_DIR  = Path.home() / ".config" / "marketcast-lab"
_ENV_FILE    = _CONFIG_DIR / ".env"

_REQUIRED_KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "FRED_API_KEY")


class SecretsError(RuntimeError):
    """秘密情報の読み込みエラー（値を含めない）。"""


def _check_permissions() -> None:
    """ディレクトリ 700 / ファイル 600 を検査する。"""
    if not _CONFIG_DIR.exists():
        raise SecretsError(
            f"設定ディレクトリが存在しません: {_CONFIG_DIR}\n"
            f"  mkdir -m 700 {_CONFIG_DIR}"
        )
    dir_mode = stat.S_IMODE(_CONFIG_DIR.stat().st_mode)
    if dir_mode != 0o700:
        raise SecretsError(
            f"設定ディレクトリの権限が不正 (現在: {oct(dir_mode)}, 必要: 0o700)\n"
            f"  chmod 700 {_CONFIG_DIR}"
        )
    if not _ENV_FILE.exists():
        raise SecretsError(
            f".env ファイルが存在しません: {_ENV_FILE}\n"
            "  以下の内容を作成してください（値は実際のキーに置き換えること）:\n"
            "  SUPABASE_URL=https://xxxx.supabase.co\n"
            "  SUPABASE_SERVICE_ROLE_KEY=eyJ...\n"
            "  FRED_API_KEY=your_key_here"
        )
    file_mode = stat.S_IMODE(_ENV_FILE.stat().st_mode)
    if file_mode != 0o600:
        raise SecretsError(
            f".env ファイルの権限が不正 (現在: {oct(file_mode)}, 必要: 0o600)\n"
            f"  chmod 600 {_ENV_FILE}"
        )


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SecretsError(f".env {lineno}行目: 'KEY=VALUE' 形式が不正")
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            result[key] = val
    return result


def load_secrets() -> dict[str, str]:
    """
    ~/.config/marketcast-lab/.env を読み込み、必須キーを返す。
    値はログへ出力しない。不足時は SecretsError を投げる。
    """
    _check_permissions()
    raw = _parse_env_file(_ENV_FILE)

    missing = [k for k in _REQUIRED_KEYS if not raw.get(k)]
    if missing:
        raise SecretsError(
            f".env に必須キーが不足しています: {', '.join(missing)}\n"
            f"  ファイル: {_ENV_FILE}"
        )

    # 値は dict にそのまま返す。呼び出し元が安全に扱うこと。
    return {k: raw[k] for k in _REQUIRED_KEYS}


def mask_secret(text: str, secrets: dict[str, str]) -> str:
    """例外メッセージやログから秘密情報の値を除去する。"""
    result = text
    for v in secrets.values():
        if v and len(v) > 8:
            result = result.replace(v, "***")
    return result
