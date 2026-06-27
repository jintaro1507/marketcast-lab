# Weekly Marketcast 有料本文 API（W3-1）

## 概要

有料会員だけが published 済み Weekly Marketcast 本文を取得できる Edge Function。

```
GET /functions/v1/get-weekly-marketcast?week_id=YYYY-WXX
```

**JWT 認証 + Subscription 認可の二重ガード。**
Subscription が `active` または `trialing` の場合のみ本文を返す。

---

## リクエスト

### エンドポイント

```
GET /functions/v1/get-weekly-marketcast?week_id=YYYY-WXX
```

### クエリパラメータ

| パラメータ | 必須 | 説明 |
|---|---|---|
| `week_id` | ✅ | ISO 週 ID（例: `2026-W26`）。実在する週のみ有効 |

### ヘッダー

| ヘッダー | 必須 | 説明 |
|---|---|---|
| `Authorization` | ✅ | `Bearer <JWT>` 形式のアクセストークン |
| `Origin` | オプション | CORS 用。許可オリジンは `SITE_URL` + ローカル開発オリジン |

---

## レスポンス

### 200 OK

```json
{
  "week_id": "2026-W26",
  "revision": 1,
  "title": "Weekly Marketcast 2026年第26週（6/22〜6/26）",
  "period_start": "2026-06-22",
  "period_end": "2026-06-26",
  "published_at": "2026-06-29T01:30:00+00:00",
  "paid_body": {
    "summary": "...",
    "asset_summaries": [...],
    "themes": [...],
    "similar_events": [...],
    "observation_points": [...],
    "disclaimer": "..."
  },
  "teaser_hash": "64文字の小文字 SHA-256",
  "paid_body_hash": "64文字の小文字 SHA-256"
}
```

**Cache-Control: `private, no-store`** — プロキシ・CDN にキャッシュされない。

### エラーレスポンス

すべてのエラーレスポンスは以下の形式:

```json
{ "error": "<error_code>" }
```

| HTTP | error_code | 説明 |
|---|---|---|
| 400 | `invalid_week_id` | week_id が指定されていない、不正な形式、実在しない週 |
| 401 | `authentication_required` | JWT がない、不正、期限切れ |
| 403 | `paid_access_required` | 有料サブスクリプションなし（フリープランを含む） |
| 404 | `weekly_report_not_found` | 指定週の published レポートが存在しない |
| 405 | `method_not_allowed` | GET / OPTIONS 以外のメソッド（`Allow: GET, OPTIONS` ヘッダーを返す） |
| 500 | `internal_error` | DB エラー、paid_body 不整合、その他内部エラー |

---

## 認証・認可フロー

```
1. OPTIONS → CORS only（DB・認証処理なし）
2. GET 以外 → 405
3. week_id 検証 → 400
4. JWT (Authorization: Bearer) → 401
5. Subscription 確認 (active / trialing のみ) → 403
6. weekly_reports WHERE week_id=? AND status='published' → 404 / 500
7. paid_body 検証 → 500
8. 200 レスポンス
```

---

## セキュリティ

| 制約 | 詳細 |
|---|---|
| paid_body 非ログ | DB から取得した paid_body はログに出力しない |
| restricted 生値遮断 | `value`, `price`, `close` 等の禁止キーが含まれる場合 500 を返す |
| Gold/S&P500 end_value | `null` でない場合 500（DB の CHECK 制約と同じ規則） |
| free ユーザー遮断 | Subscription が `active` / `trialing` でない場合は必ず 403 |
| draft/withdrawn 非返却 | `status='published'` の行のみ返す |
| service_role_key 非漏洩 | レスポンスに service role key を含めない |
| JWT 非漏洩 | エラーレスポンスにトークン・ユーザー詳細を含めない |
| RLS バイパス | weekly_reports に RLS ポリシーがないため service role client で取得 |
| キャッシュ禁止 | `Cache-Control: private, no-store` を全レスポンスに付与 |

---

## paid_body 検証（TypeScript）

DB 保存時に Python の `weekly_paid_body.schema.json` で検証済みだが、
Edge Function でも以下の実行時チェックを行う:

| 検証項目 | 条件 |
|---|---|
| 必須フィールド | `summary`, `asset_summaries`, `themes`, `similar_events`, `observation_points`, `disclaimer` |
| `asset_summaries` | 6件固定（wti/gold/sp500/ust10y/usdjpy/vix） |
| `gold` / `sp500` | `end_value` が `null` であること |
| `themes` | 1〜3件 |
| `similar_events` | 1〜5件 |
| `observation_points` | 3〜5件 |
| 禁止キー | `value`, `price`, `close`, `api_key`, `service_role_key`, `authorization`, `jwt` 等（再帰検索） |
| 数値 | NaN / Infinity を含まない |

---

## week_id 検証

- フォーマット: `YYYY-WXX`（`\d{4}-W(0[1-9]|[1-4]\d|5[0-3])`）
- W53 の実在確認: 年によって W53 が存在しない（例: 2023-W53 は無効、2020-W53 は有効）
- 複数指定不可: クエリパラメータ複数指定は 400

---

## CORS

- 許可オリジン: `SITE_URL` 環境変数から算出したオリジン + ローカル開発オリジン
- Allow-Methods: `GET, OPTIONS`（`POST, OPTIONS` を使用する他の関数とは異なる）
- 不許可オリジンには CORS ヘッダーを返さない（ブラウザがリクエストをブロック）

---

## ローカルテスト

```bash
# Deno ユニットテスト（DB・ネットワーク不使用）
deno test --allow-env supabase/functions/get-weekly-marketcast/

# ローカル Supabase 起動後の curl テスト例
curl -H "Authorization: Bearer <JWT>" \
     "http://localhost:54321/functions/v1/get-weekly-marketcast?week_id=2024-W01"
```

---

## 関連ファイル

| ファイル | 役割 |
|---|---|
| `supabase/functions/get-weekly-marketcast/index.ts` | Edge Function 本体 |
| `supabase/functions/get-weekly-marketcast/index_test.ts` | Deno ユニットテスト |
| `supabase/functions/_shared/subscription.ts` | Subscription 認可ロジック |
| `supabase/functions/_shared/cors.ts` | CORS ヘルパー（GET/OPTIONS 対応に methods 引数追加） |
| `schemas/weekly_paid_body.schema.json` | paid_body の Python 側スキーマ（保存時検証） |
| `docs/weekly-publication-operations.md` | 公開オペレーション（W2-4B） |
