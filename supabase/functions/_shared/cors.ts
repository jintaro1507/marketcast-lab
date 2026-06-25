/**
 * cors.ts — Origin ベース CORS ヘルパー
 *
 * 許可オリジン:
 *   1. SITE_URL 環境変数から算出したオリジン（本番・ステージング）
 *   2. ローカル開発用固定オリジン
 *
 * Webhook はブラウザから呼ばれないため CORS ヘッダーが不要。
 * このモジュールを Webhook ハンドラでは使用しない。
 */

/** ローカル開発環境として常に許可するオリジン */
const LOCAL_ORIGINS = new Set([
  'http://localhost:8000',
  'http://127.0.0.1:8000',
]);

/**
 * リクエストの Origin が許可リストに含まれるかを判定し、
 * 許可する場合はその Origin 文字列を、不許可の場合は null を返す。
 */
export function getAllowedOrigin(reqOrigin: string | null): string | null {
  if (!reqOrigin) return null;
  if (LOCAL_ORIGINS.has(reqOrigin)) return reqOrigin;

  const siteUrl = Deno.env.get('SITE_URL') ?? '';
  if (siteUrl) {
    try {
      if (reqOrigin === new URL(siteUrl).origin) return reqOrigin;
    } catch (_) {
      /* SITE_URL が不正な URL の場合は無視 */
    }
  }
  return null;
}

/** 許可 Origin 用の CORS レスポンスヘッダーを生成する */
export function makeCorsHeaders(allowedOrigin: string): Record<string, string> {
  return {
    'Access-Control-Allow-Origin': allowedOrigin,
    'Access-Control-Allow-Headers':
      'authorization, x-client-info, apikey, content-type',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    Vary: 'Origin',
  };
}

/**
 * OPTIONS プリフライトレスポンス。
 * 不許可 Origin には CORS ヘッダーを返さない（ブラウザがリクエストをブロックする）。
 */
export function handleOptions(reqOrigin: string | null): Response {
  const allowed = getAllowedOrigin(reqOrigin);
  if (!allowed) return new Response(null, { status: 204 });
  return new Response(null, { status: 204, headers: makeCorsHeaders(allowed) });
}
