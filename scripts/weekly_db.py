"""
Weekly Marketcast — Supabase REST クライアント

service role で weekly_asset_snapshots / weekly_reports を操作する。
キー・URL・DBレスポンス全文はログへ出力しない。
本番 URL へのテスト書き込みを拒否するガードを含む。

atomicity:
  upsert_snapshots() は 6 件を 1 つの POST リクエスト（JSON 配列）で送る。
  PostgREST は 1 リクエスト = 1 トランザクションで処理するため、
  1 件でも CHECK 制約違反が起きれば全件ロールバックされる。
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from weekly_config import PROD_PROJECT_REF, is_production_url


class WeeklyDBError(RuntimeError):
    """DB 操作エラー（秘密情報を含まない）。"""


class ProductionGuardError(WeeklyDBError):
    """本番 URL へのテスト書き込みを拒否するエラー。"""


def _build_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # macOS の場合 /etc/ssl/cert.pem を使用
    import os
    cert = "/etc/ssl/cert.pem"
    if os.path.exists(cert):
        ctx.load_verify_locations(cert)
    return ctx


_SSL_CTX = _build_ssl_ctx()


class WeeklyDB:
    def __init__(self, supabase_url: str, service_role_key: str) -> None:
        self._base = supabase_url.rstrip("/") + "/rest/v1"
        self._key  = service_role_key
        # URL は内部でのみ保持し、ログへは出さない

    def _headers(self, extra: dict | None = None) -> dict[str, str]:
        h = {
            "apikey":        self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type":  "application/json",
        }
        if extra:
            h.update(extra)
        return h

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        extra_headers: dict | None = None,
    ) -> tuple[int, Any]:
        url  = f"{self._base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req  = urllib.request.Request(
            url, data=data, headers=self._headers(extra_headers), method=method
        )
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_SSL_CTX)
        )
        try:
            with opener.open(req) as resp:
                raw = resp.read().decode()
                return resp.status, json.loads(raw) if raw else []
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            # レスポンス全文はログへ出さない。ステータスとヒントのみ。
            try:
                detail = json.loads(raw).get("message", "")[:120]
            except Exception:
                detail = raw[:120]
            raise WeeklyDBError(
                f"HTTP {e.code} {method} {path}: {detail}"
            ) from None

    # ── 読み取り ────────────────────────────────────────────────────

    def get_snapshots(self, week_id: str) -> list[dict]:
        """指定週のスナップ全件を返す（最大 6 件）。"""
        _, rows = self._request(
            "GET",
            f"/weekly_asset_snapshots?week_id=eq.{week_id}"
            "&select=week_id,asset_key,status,as_of,source,restricted,seeded",
        )
        return rows if isinstance(rows, list) else []

    def get_report(self, week_id: str) -> dict | None:
        """指定週のレポートを返す（なければ None）。"""
        _, rows = self._request(
            "GET",
            f"/weekly_reports?week_id=eq.{week_id}&select=week_id,status,revision",
        )
        return rows[0] if rows else None

    # ── 書き込み ────────────────────────────────────────────────────

    def upsert_snapshots(
        self,
        records: list[dict],
        *,
        allow_production: bool = False,
    ) -> int:
        """
        6 件のスナップを 1 リクエストで upsert する（原子的）。

        PostgREST は配列 body を 1 トランザクションで処理するため、
        1 件でも制約違反があれば全件ロールバックされる。

        allow_production=False（デフォルト）の場合、本番 URL への書き込みを拒否。
        """
        self._check_production_guard(allow_production)
        if len(records) != 6:
            raise WeeklyDBError(f"upsert_snapshots: 6件が必要ですが{len(records)}件です")

        _, rows = self._request(
            "POST",
            "/weekly_asset_snapshots",
            body=records,
            extra_headers={
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
        )
        saved = len(rows) if isinstance(rows, list) else 0
        if saved != 6:
            raise WeeklyDBError(f"保存後レコード件数が6件でない: {saved}件")
        return saved

    def insert_report_draft(
        self,
        record: dict,
        *,
        allow_production: bool = False,
    ) -> dict:
        """weekly_reports に draft レコードを INSERT する。"""
        self._check_production_guard(allow_production)
        _, rows = self._request(
            "POST",
            "/weekly_reports",
            body=record,
            extra_headers={"Prefer": "return=representation"},
        )
        if not rows:
            raise WeeklyDBError("weekly_reports INSERT に失敗しました")
        return rows[0] if isinstance(rows, list) else rows

    def delete_snapshots(self, week_id: str) -> None:
        """指定週のスナップを全件削除する（テスト用）。"""
        self._request(
            "DELETE",
            f"/weekly_asset_snapshots?week_id=eq.{week_id}",
        )

    # ── ガード ──────────────────────────────────────────────────────

    def _check_production_guard(self, allow_production: bool) -> None:
        """本番 URL への書き込みを allow_production=True なしに拒否する。"""
        # SUPABASE_URL を表示しない（本番かどうかの判定のみ行う）
        if is_production_url(self._base) and not allow_production:
            raise ProductionGuardError(
                "本番 Supabase への書き込みは allow_production=True を明示しない限り禁止です。\n"
                "save_weekly_snapshot.py で --production フラグを指定するか、\n"
                "テスト時はローカル Supabase URL を使用してください。"
            )

    def is_production(self) -> bool:
        return is_production_url(self._base)
