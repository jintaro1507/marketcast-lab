/**
 * get-similar-matches
 *
 * 有料会員向け Protected API。
 * 現在の市場環境（current_context_public.json）と過去イベントデータ（event_reactions.json）を
 * サーバー側で照合し、指定 cause_tag に対する top5 類似局面を返す。
 *
 * 認証フロー:
 *   1. JWT 検証（Authorization ヘッダー → getSupabaseUserClient → auth.getUser()）
 *   2. Subscription 認可（getSubscriptionAccess → allowed=true のみ続行）
 *   3. cause_tag バリデーション
 *   4. SITE_URL からデータ取得
 *   5. マッチング計算
 *   6. top5 を返却
 *
 * セキュリティ:
 *   - クライアントから user_id / market_state_tags / URL を受け取らない
 *   - DB 情報・JWT・Subscription 詳細をレスポンスに含めない
 *   - 例外メッセージをクライアントに返さない（ログにのみ記録）
 */

import { handleOptions } from '../_shared/cors.ts';
import { jsonOk, jsonError } from '../_shared/response.ts';
import { getSupabaseAdmin, getSupabaseUserClient } from '../_shared/supabase.ts';
import { getSubscriptionAccess } from '../_shared/subscription.ts';
import { computeMatches, type ScoredEvent } from './matching.ts';
import { computeEventTimeline, DISCLAIMER, type AssetTimeline } from '../_shared/timeline.ts';

// ─── 内部型定義 ───────────────────────────────────────────────────────────────

interface CurrentContextJson {
  market_state_tags?: Record<string, string>;
  data_completeness?: number;
  generated_at?: string;
}

interface EventReactionsJson {
  cause_tag_labels?: Record<string, string>;
  events?: unknown[];
}

// ─── 設定 ────────────────────────────────────────────────────────────────────

/** データ取得のタイムアウト（ミリ秒） */
const FETCH_TIMEOUT_MS = 10_000;

// ─── ハンドラ ─────────────────────────────────────────────────────────────────

Deno.serve(async (req: Request): Promise<Response> => {
  const origin = req.headers.get('Origin');

  // ── 1. CORS preflight ────────────────────────────────────────────────────
  if (req.method === 'OPTIONS') return handleOptions(origin);

  // ── 2. POST のみ受け付ける ────────────────────────────────────────────────
  if (req.method !== 'POST') return jsonError(405, 'Method not allowed', origin);

  // ── 3. JWT 認証 ──────────────────────────────────────────────────────────
  const authHeader = req.headers.get('Authorization') ?? '';
  const supabaseUser = getSupabaseUserClient(authHeader);
  const {
    data: { user },
    error: authError,
  } = await supabaseUser.auth.getUser();

  if (authError || !user) return jsonError(401, 'Unauthorized', origin);

  // ── 4. Subscription 認可 ──────────────────────────────────────────────────
  const supabaseAdmin = getSupabaseAdmin();
  let allowed = false;
  try {
    const result = await getSubscriptionAccess(supabaseAdmin, user.id);
    if (!result.allowed) {
      return jsonError(403, 'Paid subscription required', origin);
    }
    allowed = true;
  } catch (e) {
    console.error('[get-similar-matches] subscription check error:', (e as Error).message);
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  // ── 5. リクエスト body パース ─────────────────────────────────────────────
  // allowed フラグを TypeScript が認識できるよう明示的に確認
  if (!allowed) return jsonError(403, 'Paid subscription required', origin);

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

  // ── 6. cause_tag 形式検証（existence/type のみ。allowlist は data 取得後） ──
  const causeTag = body.cause_tag;
  if (typeof causeTag !== 'string' || causeTag.trim().length === 0) {
    return jsonError(400, 'cause_tag is required', origin);
  }

  // ── 7. データ取得 ─────────────────────────────────────────────────────────
  const rawSiteUrl = Deno.env.get('SITE_URL') ?? '';
  const siteUrl = rawSiteUrl.replace(/\/$/, '');

  if (!siteUrl) {
    console.error('[get-similar-matches] SITE_URL is not configured');
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  let currentCtx: CurrentContextJson;
  let eventReactions: EventReactionsJson;

  try {
    const signal = AbortSignal.timeout(FETCH_TIMEOUT_MS);
    const [ctxResp, erResp] = await Promise.all([
      fetch(`${siteUrl}/data/current_context_public.json`, { signal }),
      fetch(`${siteUrl}/data/event_reactions.json`, { signal }),
    ]);

    if (!ctxResp.ok) {
      console.error(
        '[get-similar-matches] current_context_public.json fetch failed:',
        ctxResp.status,
      );
      return jsonError(503, 'Service temporarily unavailable', origin);
    }
    if (!erResp.ok) {
      console.error(
        '[get-similar-matches] event_reactions.json fetch failed:',
        erResp.status,
      );
      return jsonError(503, 'Service temporarily unavailable', origin);
    }

    [currentCtx, eventReactions] = await Promise.all([
      ctxResp.json() as Promise<CurrentContextJson>,
      erResp.json() as Promise<EventReactionsJson>,
    ]);
  } catch (e) {
    console.error('[get-similar-matches] data fetch error:', (e as Error).message);
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  // ── 8. cause_tag を allowlist で検証（event_reactions.json の cause_tag_labels） ──
  const causeTagLabels = eventReactions.cause_tag_labels;
  if (
    typeof causeTagLabels !== 'object' ||
    causeTagLabels === null ||
    !(causeTag in causeTagLabels)
  ) {
    return jsonError(400, 'Invalid cause_tag', origin);
  }

  // ── 9. 現在市場環境の検証 ──────────────────────────────────────────────────
  const marketStateTags = currentCtx.market_state_tags;
  const dataCompleteness = currentCtx.data_completeness;

  if (typeof dataCompleteness !== 'number' || dataCompleteness < 2) {
    console.error(
      '[get-similar-matches] data_completeness insufficient:',
      dataCompleteness,
    );
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  if (
    !marketStateTags ||
    typeof marketStateTags !== 'object' ||
    Array.isArray(marketStateTags)
  ) {
    console.error('[get-similar-matches] market_state_tags invalid');
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  // ── 10. events 配列の検証 ──────────────────────────────────────────────────
  if (!Array.isArray(eventReactions.events)) {
    console.error('[get-similar-matches] events is not an array');
    return jsonError(503, 'Service temporarily unavailable', origin);
  }

  // ── 11. 類似度計算 ────────────────────────────────────────────────────────
  const events = eventReactions.events as ScoredEvent[];
  const matches = computeMatches(causeTag, marketStateTags, events);

  // ── 11. Timeline 計算（各マッチに方向データを付与） ───────────────────────
  // computeEventTimeline は純粋関数で外部IOなし。Top5件の計算コスト軽微。
  const enrichedMatches = matches.map((m) => {
    let timeline: AssetTimeline[] | null = null;
    try {
      const computed = computeEventTimeline({ reactions: m.reactions });
      // 有効資産0件は null として返す。空配列を有効なTimelineとして扱わない。
      timeline = computed.length > 0 ? computed : null;
    } catch (_) {
      // timeline計算失敗はマッチングレスポンス全体を壊さない
    }
    return { ...m, timeline };
  });

  // ── 12. レスポンス ────────────────────────────────────────────────────────
  // 内部 DB 情報（subscription/user_id/JWT）は含めない
  return jsonOk(
    {
      cause_tag: causeTag,
      generated_at: new Date().toISOString(),
      current_market_state: marketStateTags,
      scoring: {
        max_score: Object.keys(marketStateTags).length,
        method: 'market_state_tag_overlap',
      },
      timeline_disclaimer: DISCLAIMER,
      matches: enrichedMatches,
    },
    origin,
  );
});
