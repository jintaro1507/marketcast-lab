/**
 * create-checkout-session
 *
 * ログイン済みユーザー向けに Stripe Checkout Session を作成する。
 *
 * Stripe Customer 解決順序（重複作成防止）:
 *   1. stripe_customers.user_id から stripe_customer_id 取得（一次ソース）
 *   2. subscriptions.stripe_customer_id（後方互換：stripe_customers 導入前の既存行）
 *   3. Stripe Customer Search（metadata.supabase_user_id 一致）
 *      ※ Search インデックスの遅延で空振りする場合あり → Step 4 へフォールスルー
 *   4. Stripe Customer 新規作成（Idempotency Key: create-customer-{user_id}）
 *
 * 解決後は必ず stripe_customers へ upsert し、次回以降 Step 1 で解決できるようにする。
 * upsert で unique violation（23505）が発生した場合は保存済みの Customer ID を再取得して使用する。
 */

import Stripe from 'npm:stripe@17.7.0';
import { handleOptions } from '../_shared/cors.ts';
import { jsonOk, jsonError } from '../_shared/response.ts';
import { getStripe } from '../_shared/stripe.ts';
import { getSupabaseAdmin, getSupabaseUserClient } from '../_shared/supabase.ts';
import { SupabaseClient } from 'npm:@supabase/supabase-js@2';

/** Supabase が発行する user.id は常に UUID 形式。クエリ埋め込み前に検証する。 */
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

Deno.serve(async (req: Request): Promise<Response> => {
  const origin = req.headers.get('Origin');

  if (req.method === 'OPTIONS') return handleOptions(origin);
  if (req.method !== 'POST') return jsonError(405, 'Method not allowed', origin);

  // ── 1. JWT 認証 ──────────────────────────────────────────────────────────
  const authHeader = req.headers.get('Authorization') ?? '';
  const supabaseUser = getSupabaseUserClient(authHeader);
  const {
    data: { user },
    error: authError,
  } = await supabaseUser.auth.getUser();

  if (authError || !user) return jsonError(401, 'Unauthorized', origin);

  // ── 2. DB 参照（stripe_customers + subscriptions を並列取得） ────────────
  const supabaseAdmin = getSupabaseAdmin();
  const [customerResult, subResult] = await Promise.all([
    supabaseAdmin
      .from('stripe_customers')
      .select('stripe_customer_id')
      .eq('user_id', user.id)
      .maybeSingle(),
    supabaseAdmin
      .from('subscriptions')
      .select('stripe_customer_id, status')
      .eq('user_id', user.id)
      .maybeSingle(),
  ]);

  // 二重契約防止
  const existingStatus = subResult.data?.status;
  if (existingStatus === 'active' || existingStatus === 'trialing') {
    return jsonError(409, 'Already subscribed', origin);
  }

  // ── 3. 環境変数確認 ───────────────────────────────────────────────────────
  const priceId = Deno.env.get('STRIPE_PRICE_ID');
  if (!priceId) return jsonError(500, 'Price not configured', origin);

  const siteUrl = Deno.env.get('SITE_URL') ?? '';

  let stripe: Stripe;
  try {
    stripe = getStripe();
  } catch (_) {
    return jsonError(500, 'Payment service not configured', origin);
  }

  // ── 4. Stripe Customer 解決 ───────────────────────────────────────────────
  if (!UUID_RE.test(user.id)) {
    return jsonError(400, 'Invalid user ID format', origin);
  }

  let customerId = await resolveCustomerId(
    user.id,
    user.email,
    customerResult.data?.stripe_customer_id ?? null,
    subResult.data?.stripe_customer_id ?? null,
    stripe,
  );

  if (!customerId) {
    return jsonError(502, 'Failed to create billing account', origin);
  }

  // ── 5. stripe_customers へ保存（以降の Checkout で Step 1 解決可能にする） ──
  // saveStripeCustomer は保存失敗・再取得失敗時に throw する。
  // 未保存の Customer ID を Checkout Session に使用させない。
  let confirmedCustomerId: string;
  try {
    confirmedCustomerId = await saveStripeCustomer(user.id, customerId, supabaseAdmin);
  } catch (err) {
    console.error(
      '[create-checkout-session] saveStripeCustomer failed',
      (err as Error).message,
    );
    return jsonError(502, 'Failed to confirm billing account', origin);
  }

  // ── 6. Checkout Session 作成 ──────────────────────────────────────────────
  // client_reference_id: Webhook で user_id を特定する主要経路
  // metadata: Session オブジェクトから user_id を参照可能にする補助
  // subscription_data.metadata: Subscription オブジェクト自体に user_id を埋め込む
  //   → customer.subscription.created 単独着弾時に resolve 可能
  let session: Stripe.Checkout.Session;
  try {
    session = await stripe.checkout.sessions.create({
      customer: confirmedCustomerId,
      client_reference_id: user.id,
      mode: 'subscription',
      line_items: [{ price: priceId, quantity: 1 }],
      metadata: {
        supabase_user_id: user.id,
      },
      subscription_data: {
        metadata: {
          supabase_user_id: user.id,
        },
      },
      success_url: `${siteUrl}/checkout-success.html?session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${siteUrl}/pricing.html`,
    });
  } catch (_) {
    return jsonError(502, 'Failed to create checkout session', origin);
  }

  return jsonOk({ url: session.url }, origin);
});

// ─────────────────────────────────────────────────────────────────────────────
// Customer 解決
// ─────────────────────────────────────────────────────────────────────────────

/**
 * 4 段階の優先順でStripe Customer IDを解決する。
 * Step 1/2 は呼び出し元で取得済みの値を受け取る（DB 往復を最小化）。
 */
async function resolveCustomerId(
  userId: string,
  userEmail: string | undefined,
  fromStripeCustomers: string | null,
  fromSubscriptions: string | null,
  stripe: Stripe,
): Promise<string | null> {
  // Step 1: stripe_customers テーブル（一次ソース）
  if (fromStripeCustomers) return fromStripeCustomers;

  // Step 2: subscriptions テーブル（後方互換）
  if (fromSubscriptions) return fromSubscriptions;

  // Step 3: Stripe Customer Search（Search インデックス遅延で空振りあり）
  try {
    const searchResult = await stripe.customers.search({
      query: `metadata['supabase_user_id']:'${userId}'`,
      limit: 1,
    });
    if (searchResult.data.length > 0) {
      const found = searchResult.data[0];
      // email や metadata が欠落している場合のみ補完（不要な更新を避ける）
      const needsUpdate =
        (userEmail && found.email !== userEmail) ||
        !found.metadata?.supabase_user_id;
      if (needsUpdate) {
        await stripe.customers.update(found.id, {
          email: userEmail,
          metadata: { supabase_user_id: userId },
        });
      }
      return found.id;
    }
  } catch (_) {
    /* Search 失敗時は Step 4 の新規作成へフォールスルー */
  }

  // Step 4: 新規作成
  // Idempotency Key により同一 user_id の並列リクエストが同一 Customer を返すことを保証する
  try {
    const customer = await stripe.customers.create(
      { email: userEmail, metadata: { supabase_user_id: userId } },
      { idempotencyKey: `create-customer-${userId}` },
    );
    return customer.id;
  } catch (_) {
    return null;
  }
}

/**
 * stripe_customers テーブルへ Customer 対応を保存し、使用すべき Customer ID を返す。
 *
 * - 成功時: 引数の customerId をそのまま返す
 * - 23505（stripe_customer_id unique 違反）: user_id で再取得した ID を返す
 *   保存済みの先着 ID を優先し、別ユーザーの ID が使われることはない（user_id で絞込）
 * - 再取得失敗: throw → 呼び出し元が 502 を返し Checkout を中止する
 * - その他の保存失敗: throw → 未確認の Customer ID を Checkout Session に使わせない
 */
async function saveStripeCustomer(
  userId: string,
  customerId: string,
  supabase: SupabaseClient,
): Promise<string> {
  const { error } = await supabase
    .from('stripe_customers')
    .upsert(
      { user_id: userId, stripe_customer_id: customerId },
      { onConflict: 'user_id' },
    );

  if (!error) return customerId;

  if (error.code === '23505') {
    // 別の Customer ID がすでにこの user_id に紐づいて保存済み → 保存済みを優先する
    const { data: stored } = await supabase
      .from('stripe_customers')
      .select('stripe_customer_id')
      .eq('user_id', userId)
      .maybeSingle();
    if (stored?.stripe_customer_id) return stored.stripe_customer_id;
    // 再取得失敗 → Checkout 作成を中止する（未確認 ID は使わない）
    throw new Error('stripe_customers refetch failed after 23505');
  }

  // その他の保存失敗 → 未確認の Customer ID を Checkout に使わせない
  throw new Error(`stripe_customers upsert failed: ${error.code}`);
}
