/**
 * get-event-reaction-timeline
 *
 * 有料会員向け Protected API。
 * 指定した event_id について、各資産の価格変化方向・中期反転フラグを返す。
 *
 * 認証フロー:
 *   1. JWT 検証（Authorization ヘッダー）
 *   2. Subscription 認可（active/trialing のみ）
 *   3. event_id バリデーション
 *   4. SITE_URL から event_reactions.json を取得
 *   5. _shared/timeline.ts で方向・反転を計算
 *   6. レスポンス返却
 *
 * セキュリティ:
 *   - クライアントから user_id・URL を受け取らない
 *   - DB情報・JWT・Subscription詳細をレスポンスに含めない
 *   - 例外メッセージをクライアントに返さない
 *   - event_id による IDOR は構造的に存在しない
 *     （全イベントデータは公開JSONに存在し、ユーザー固有データではない）
 */

import { handleOptions } from '../_shared/cors.ts';
import { jsonOk, jsonError } from '../_shared/response.ts';
import { getSupabaseAdmin, getSupabaseUserClient } from '../_shared/supabase.ts';
import { getSubscriptionAccess } from '../_shared/subscription.ts';
import {
  computeEventTimeline,
  DISCLAIMER,
  type EventLike,
} from '../_shared/timeline.ts';

const FETCH_TIMEOUT_MS = 10_000;
const EVENT_ID_MAX_LEN = 100;

Deno.serve(async (req: Request): Promise<Response> => {
  const origin = req.headers.get('Origin');

  // ── 1. CORS preflight ────────────────────────────────────────────────
  if (req.method === 'OPTIONS') return handleOptions(origin);

  // ── 2. POST のみ受け付ける ────────────────────────────────────────────
  if (req.method !== 'POST') return jsonError(405, 'Method not allowed', origin);

  // ── 3. JWT 認証 ──────────────────────────────────────────────────────
  const authHeader = req.headers.get('Authorization') ?? '';
  const supabaseUser = getSupabaseUserClient(authHeader);
  const {
    data: { user },
    error: authError,
  } = await supabaseUser.auth.getUser();

  if (authError || !user) return jsonError(401, 'Unauthorized', origin);

  // ── 4. Subscription 認可 ──────────────────────────────────────────────
  const supabaseAdmin = getSupabaseAdmin();
  try {
    const result = await getSubscriptionAccess(supabaseAdmin, user.id);
    if (!result.allowed) {
      return jsonError(403, 'Paid subscription required', origin);
    }
  } catch (e) {
    console.error(
      '[get-event-reaction-timeline] subscription check error:',
      (e as Error).message,
    );
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  // ── 5. リクエスト body パース ─────────────────────────────────────────
  let body: Record<string, unknown>;
  try {
    const raw = await req.json();
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
      return jsonError(400, 'Invalid request body', origin);
    }
    body = raw as Record<string, unknown>;
  } catch {
    return jsonError(400, 'Invalid JSON', origin);
  }

  // ── 6. event_id バリデーション ────────────────────────────────────────
  const eventId = body.event_id;
  if (typeof eventId !== 'string' || eventId.trim().length === 0) {
    return jsonError(400, 'event_id is required', origin);
  }
  if (eventId.length > EVENT_ID_MAX_LEN) {
    return jsonError(400, 'event_id is too long', origin);
  }
  const sanitizedEventId = eventId.trim();

  // ── 7. event_reactions.json 取得 ─────────────────────────────────────
  const rawSiteUrl = Deno.env.get('SITE_URL') ?? '';
  const siteUrl = rawSiteUrl.replace(/\/$/, '');

  if (!siteUrl) {
    console.error('[get-event-reaction-timeline] SITE_URL is not configured');
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  let eventReactionsData: { events?: unknown[] };
  try {
    const signal = AbortSignal.timeout(FETCH_TIMEOUT_MS);
    const resp = await fetch(`${siteUrl}/data/event_reactions.json`, { signal });
    if (!resp.ok) {
      console.error(
        '[get-event-reaction-timeline] event_reactions.json fetch failed:',
        resp.status,
      );
      return jsonError(503, 'Service temporarily unavailable', origin);
    }
    eventReactionsData = await resp.json() as { events?: unknown[] };
  } catch (e) {
    console.error(
      '[get-event-reaction-timeline] data fetch error:',
      (e as Error).message,
    );
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  // ── 8. events 配列の検証 ──────────────────────────────────────────────
  if (!Array.isArray(eventReactionsData.events)) {
    console.error('[get-event-reaction-timeline] events is not an array');
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  // ── 9. イベント検索 ───────────────────────────────────────────────────
  type EventRecord = EventLike & { id?: string; name?: string; date?: string };
  const event = (eventReactionsData.events as EventRecord[]).find(
    (e) => typeof e.id === 'string' && e.id === sanitizedEventId,
  );

  if (!event) {
    return jsonError(404, 'Event not found', origin);
  }

  // ── 10. Timeline 計算 ─────────────────────────────────────────────────
  const assets = computeEventTimeline(event);

  // 有効資産0件は上流データ不整合として扱う。
  // 空配列を正常な有料レスポンスとして返さない。
  if (assets.length === 0) {
    console.warn(
      '[get-event-reaction-timeline] no valid assets for event:',
      sanitizedEventId,
    );
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  // ── 11. レスポンス ────────────────────────────────────────────────────
  return jsonOk(
    {
      event_id: event.id ?? sanitizedEventId,
      name: typeof event.name === 'string' ? event.name : '',
      date: typeof event.date === 'string' ? event.date : '',
      generated_at: new Date().toISOString(),
      assets,
      disclaimer: DISCLAIMER,
    },
    origin,
  );
});
