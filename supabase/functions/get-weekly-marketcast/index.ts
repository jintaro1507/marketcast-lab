/**
 * get-weekly-marketcast
 *
 * 有料会員向け Protected API。
 * 指定週の published Weekly Marketcast 有料本文を返す。
 *
 * 認証フロー:
 *   1. OPTIONS preflight → CORS のみ（DB・認証処理なし）
 *   2. GET のみ受け付ける（他は 405）
 *   3. week_id クエリパラメータ検証（400）
 *   4. JWT 認証（401）
 *   5. Subscription 認可（403）
 *   6. DB から published 行を取得（404/500）
 *   7. paid_body 検証（500）
 *   8. 必要フィールドのみ返却（Cache-Control: private, no-store）
 *
 * セキュリティ:
 *   - service role key をクライアントへ返さない
 *   - JWT・Subscription 詳細をレスポンスに含めない
 *   - paid_body をログに出力しない
 *   - restricted 生値（price, close, value 等）をキー検査して遮断
 *   - gold/sp500 の end_value が null であることを確認
 */

import { getAllowedOrigin, handleOptions } from '../_shared/cors.ts';
import { getSupabaseAdmin, getSupabaseUserClient } from '../_shared/supabase.ts';
import { getSubscriptionAccess, type SubscriptionAccessResult } from '../_shared/subscription.ts';

// ─── 定数 ─────────────────────────────────────────────────────────────────────

const ALLOWED_METHODS = 'GET, OPTIONS';

const WEEK_ID_RE = /^(\d{4})-W(0[1-9]|[1-4]\d|5[0-3])$/;

/** restricted 生値として禁止するキー名 */
const FORBIDDEN_RAW_KEYS = new Set([
  'value',
  'current_value',
  'previous_value',
  'latest_value',
  'raw_value',
  'price',
  'close',
  'api_key',
  'service_role_key',
  'authorization',
  'jwt',
]);

/** end_value を null 必須とする asset_key */
const NULL_END_VALUE_ASSETS = new Set(['gold', 'sp500']);

// ─── ISO 週番号検証 ────────────────────────────────────────────────────────────

function getISOWeekNumber(date: Date): number {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  const day = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  return Math.ceil(((d.valueOf() - yearStart.valueOf()) / 86_400_000 + 1) / 7);
}

/**
 * week_id が実在する ISO 週かを検証する。
 * フォーマット: YYYY-WXX（W01〜W53）
 * W53 は年によって存在しない（例: 2023-W53 は無効、2020-W53 は有効）。
 */
export function isValidWeekId(weekId: string): boolean {
  const m = weekId.match(WEEK_ID_RE);
  if (!m) return false;
  const year = parseInt(m[1], 10);
  const week = parseInt(m[2], 10);
  if (week < 53) return true;
  // W53: 12月28日が常にその年の最終週に含まれるため、ISOWeek が 53 かを確認する
  return getISOWeekNumber(new Date(year, 11, 28)) === 53;
}

// ─── セキュリティ検査 ─────────────────────────────────────────────────────────

/** オブジェクト/配列を再帰的に走査して NaN/Infinity を検出する */
export function hasNonFiniteNumbers(obj: unknown): boolean {
  if (typeof obj === 'number') return !isFinite(obj);
  if (Array.isArray(obj)) return obj.some(hasNonFiniteNumbers);
  if (obj !== null && typeof obj === 'object') {
    return Object.values(obj as Record<string, unknown>).some(hasNonFiniteNumbers);
  }
  return false;
}

/** オブジェクト/配列を再帰的に走査して禁止キーを検出する */
export function hasForbiddenKey(obj: unknown): boolean {
  if (Array.isArray(obj)) return obj.some(hasForbiddenKey);
  if (obj !== null && typeof obj === 'object') {
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      if (FORBIDDEN_RAW_KEYS.has(k)) return true;
      if (hasForbiddenKey(v)) return true;
    }
  }
  return false;
}

// ─── paid_body 検証 ─────────────────────────────────────────────────────────

export interface PaidBodyError {
  field: string;
  message: string;
}

/**
 * paid_body の内容を TypeScript で検証する。
 * Python の weekly_paid_body.schema.json に相当する実行時サニティチェック。
 * フル schema 検証（Python 側）は保存時に完了しているため、ここでは要点のみ確認する。
 */
export function validatePaidBody(paidBody: unknown): PaidBodyError[] {
  const errors: PaidBodyError[] = [];

  if (typeof paidBody !== 'object' || paidBody === null || Array.isArray(paidBody)) {
    errors.push({ field: 'paid_body', message: 'must be a non-null object' });
    return errors;
  }

  const body = paidBody as Record<string, unknown>;

  // 必須フィールド存在確認
  for (const field of ['summary', 'asset_summaries', 'themes', 'similar_events', 'observation_points', 'disclaimer'] as const) {
    if (!(field in body)) {
      errors.push({ field, message: 'required field missing' });
    }
  }

  // summary: 文字列
  if ('summary' in body && typeof body.summary !== 'string') {
    errors.push({ field: 'summary', message: 'must be a string' });
  }

  // asset_summaries: 6件固定 + gold/sp500 の end_value null 確認
  if ('asset_summaries' in body) {
    if (!Array.isArray(body.asset_summaries)) {
      errors.push({ field: 'asset_summaries', message: 'must be an array' });
    } else if (body.asset_summaries.length !== 6) {
      errors.push({
        field: 'asset_summaries',
        message: `must have exactly 6 items, got ${body.asset_summaries.length}`,
      });
    } else {
      for (const item of body.asset_summaries) {
        if (typeof item !== 'object' || item === null) continue;
        const summary = item as Record<string, unknown>;
        const key = summary.asset_key;
        if (typeof key === 'string' && NULL_END_VALUE_ASSETS.has(key) && summary.end_value !== null) {
          errors.push({
            field: `asset_summaries[${key}].end_value`,
            message: `must be null for restricted asset (got ${summary.end_value})`,
          });
        }
      }
    }
  }

  // themes: 1〜3件
  if ('themes' in body) {
    if (!Array.isArray(body.themes)) {
      errors.push({ field: 'themes', message: 'must be an array' });
    } else if (body.themes.length < 1 || body.themes.length > 3) {
      errors.push({ field: 'themes', message: `must have 1-3 items, got ${body.themes.length}` });
    }
  }

  // similar_events: 1〜5件
  if ('similar_events' in body) {
    if (!Array.isArray(body.similar_events)) {
      errors.push({ field: 'similar_events', message: 'must be an array' });
    } else if (body.similar_events.length < 1 || body.similar_events.length > 5) {
      errors.push({
        field: 'similar_events',
        message: `must have 1-5 items, got ${body.similar_events.length}`,
      });
    }
  }

  // observation_points: 3〜5件
  if ('observation_points' in body) {
    if (!Array.isArray(body.observation_points)) {
      errors.push({ field: 'observation_points', message: 'must be an array' });
    } else if (body.observation_points.length < 3 || body.observation_points.length > 5) {
      errors.push({
        field: 'observation_points',
        message: `must have 3-5 items, got ${body.observation_points.length}`,
      });
    }
  }

  // disclaimer: 文字列
  if ('disclaimer' in body && typeof body.disclaimer !== 'string') {
    errors.push({ field: 'disclaimer', message: 'must be a string' });
  }

  // 禁止キー検査（restricted 生値）
  if (hasForbiddenKey(body)) {
    errors.push({ field: 'paid_body', message: 'contains forbidden raw value key' });
  }

  // NaN / Infinity 検査
  if (hasNonFiniteNumbers(body)) {
    errors.push({ field: 'paid_body', message: 'contains NaN or Infinity' });
  }

  return errors;
}

// ─── DB 型定義 ─────────────────────────────────────────────────────────────────

export interface WeeklyReportRow {
  week_id: string;
  revision: number;
  title: string;
  period_start: string;
  period_end: string;
  published_at: string;
  paid_body: unknown;
  teaser_hash: string;
  paid_body_hash: string;
}

// ─── レスポンスヘルパー ────────────────────────────────────────────────────────

function buildCorsHeaders(origin: string | null): Record<string, string> {
  if (!origin) return {};
  const allowed = getAllowedOrigin(origin);
  if (!allowed) return {};
  return {
    'Access-Control-Allow-Origin': allowed,
    'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
    'Access-Control-Allow-Methods': ALLOWED_METHODS,
    Vary: 'Origin',
  };
}

function jsonResponse(
  status: number,
  body: unknown,
  origin: string | null,
  extraHeaders?: Record<string, string>,
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Cache-Control': 'private, no-store',
      ...buildCorsHeaders(origin),
      ...(extraHeaders ?? {}),
    },
  });
}

// ─── 依存性注入型 ─────────────────────────────────────────────────────────────

export type GetUserResult = {
  data: { user: { id: string } | null };
  error: { message: string } | null;
};

export type GetUserFn = (authHeader: string) => Promise<GetUserResult>;
export type GetSubscriptionAccessFn = (userId: string) => Promise<SubscriptionAccessResult>;
export type FetchReportFn = (weekId: string) => Promise<WeeklyReportRow | null>;

export interface HandlerDeps {
  getUser: GetUserFn;
  getSubscriptionAccess: GetSubscriptionAccessFn;
  fetchReport: FetchReportFn;
}

// ─── ハンドラ（テスト可能な純粋関数） ────────────────────────────────────────

export async function handleRequest(req: Request, deps: HandlerDeps): Promise<Response> {
  const reqId = crypto.randomUUID().slice(0, 8);
  const origin = req.headers.get('Origin');

  // ── 1. OPTIONS preflight ────────────────────────────────────────────────────
  if (req.method === 'OPTIONS') return handleOptions(origin, ALLOWED_METHODS);

  // ── 2. GET のみ受け付ける ──────────────────────────────────────────────────
  if (req.method !== 'GET') {
    return jsonResponse(405, { error: 'method_not_allowed' }, origin, { Allow: ALLOWED_METHODS });
  }

  // ── 3. week_id クエリパラメータ検証 ───────────────────────────────────────
  const url = new URL(req.url);
  const weekIdValues = url.searchParams.getAll('week_id');

  if (weekIdValues.length !== 1 || weekIdValues[0] === '') {
    console.log(`[get-weekly-marketcast] [${reqId}] 400 invalid_week_id count=${weekIdValues.length}`);
    return jsonResponse(400, { error: 'invalid_week_id' }, origin);
  }

  const weekId = weekIdValues[0];
  if (!isValidWeekId(weekId)) {
    console.log(`[get-weekly-marketcast] [${reqId}] 400 invalid_week_id value=${weekId}`);
    return jsonResponse(400, { error: 'invalid_week_id' }, origin);
  }

  // ── 4. JWT 認証 ────────────────────────────────────────────────────────────
  const authHeader = req.headers.get('Authorization') ?? '';
  if (!authHeader.startsWith('Bearer ')) {
    console.log(`[get-weekly-marketcast] [${reqId}] 401 no bearer week=${weekId}`);
    return jsonResponse(401, { error: 'authentication_required' }, origin);
  }

  const userResult = await deps.getUser(authHeader);
  if (userResult.error || !userResult.data.user) {
    console.log(`[get-weekly-marketcast] [${reqId}] 401 auth failed week=${weekId}`);
    return jsonResponse(401, { error: 'authentication_required' }, origin);
  }

  const userId = userResult.data.user.id;

  // ── 5. Subscription 認可 ───────────────────────────────────────────────────
  let subResult: SubscriptionAccessResult;
  try {
    subResult = await deps.getSubscriptionAccess(userId);
  } catch (e) {
    console.error(
      `[get-weekly-marketcast] [${reqId}] subscription error week=${weekId}: ${(e as Error).message}`,
    );
    return jsonResponse(500, { error: 'internal_error' }, origin);
  }

  if (!subResult.allowed) {
    console.log(
      `[get-weekly-marketcast] [${reqId}] 403 paid_access_required week=${weekId} reason=${subResult.reason}`,
    );
    return jsonResponse(403, { error: 'paid_access_required' }, origin);
  }

  // ── 6. DB から published 行を取得 ──────────────────────────────────────────
  let report: WeeklyReportRow | null;
  try {
    report = await deps.fetchReport(weekId);
  } catch (e) {
    console.error(
      `[get-weekly-marketcast] [${reqId}] db error week=${weekId}: ${(e as Error).message}`,
    );
    return jsonResponse(500, { error: 'internal_error' }, origin);
  }

  if (report === null) {
    console.log(`[get-weekly-marketcast] [${reqId}] 404 not_found week=${weekId}`);
    return jsonResponse(404, { error: 'weekly_report_not_found' }, origin);
  }

  // ── 7. paid_body 検証 ──────────────────────────────────────────────────────
  const bodyErrors = validatePaidBody(report.paid_body);
  if (bodyErrors.length > 0) {
    // paid_body 内容はログに出さない（restricted 生値が含まれる可能性）
    console.error(
      `[get-weekly-marketcast] [${reqId}] paid_body validation failed week=${weekId} errors=${bodyErrors.length}`,
    );
    return jsonResponse(500, { error: 'internal_error' }, origin);
  }

  // ── 8. レスポンス（必要フィールドのみ・paid_body 含む） ─────────────────────
  console.log(`[get-weekly-marketcast] [${reqId}] 200 ok week=${weekId}`);
  return jsonResponse(
    200,
    {
      week_id:        report.week_id,
      revision:       report.revision,
      title:          report.title,
      period_start:   report.period_start,
      period_end:     report.period_end,
      published_at:   report.published_at,
      paid_body:      report.paid_body,
      teaser_hash:    report.teaser_hash,
      paid_body_hash: report.paid_body_hash,
    },
    origin,
  );
}

// ─── 本番依存性 + Deno.serve ──────────────────────────────────────────────────

if (import.meta.main) Deno.serve((req: Request): Promise<Response> => {
  const supabaseAdmin = getSupabaseAdmin();
  return handleRequest(req, {
    getUser: (authHeader) =>
      getSupabaseUserClient(authHeader).auth.getUser() as Promise<GetUserResult>,
    getSubscriptionAccess: (userId) => getSubscriptionAccess(supabaseAdmin, userId),
    fetchReport: async (weekId) => {
      const { data, error } = await supabaseAdmin
        .from('weekly_reports')
        .select(
          'week_id, revision, title, period_start, period_end, published_at, paid_body, teaser_hash, paid_body_hash',
        )
        .eq('week_id', weekId)
        .eq('status', 'published');

      if (error) {
        throw new Error(`weekly_reports SELECT failed: ${error.message} (code: ${error.code})`);
      }
      if (!data || data.length === 0) return null;
      if (data.length > 1) {
        throw new Error(`Unexpected ${data.length} published rows for week_id=${weekId}`);
      }
      return data[0] as WeeklyReportRow;
    },
  });
});
