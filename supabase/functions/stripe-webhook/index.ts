/**
 * stripe-webhook
 *
 * Stripe Webhook を受信し subscriptions テーブルを同期する。
 * ブラウザから呼ばれないため CORS ヘッダーは不要。
 *
 * ── 状態巻き戻り防止 ─────────────────────────────────────────────────────
 *   customer.subscription.created / updated では event.data.object の状態を
 *   そのまま DB に書かない。必ず stripe.subscriptions.retrieve() で Stripe 上の
 *   最新状態を取得してから upsert する。
 *   古いイベントが後着しても、retrieve() は現在の状態を返すため巻き戻りが起きない。
 *
 *   customer.subscription.deleted は Stripe 側でも削除済みのため retrieve は
 *   行わず、event のサブスクリプション ID で行を特定して status='canceled' に更新する。
 *   user_id + stripe_subscription_id の両方で絞り込むことで、再購読後の行を
 *   誤って canceled にしない。
 *
 * ── 冪等性 ──────────────────────────────────────────────────────────────
 *   処理成功後に event_id を stripe_webhook_events へ INSERT する。
 *   失敗時は記録しないため Stripe が再送できる。
 *   同時受信で競合 INSERT（unique violation: 23505）が発生した場合は
 *   処理済み相当として 200 を返し、不要な Stripe 再送を防ぐ。
 *
 * ── customer.subscription.deleted の行削除不採用 ─────────────────────────
 *   「行なし = free（未契約）」の設計原則を維持する。
 *   解約済みユーザーは status='canceled' で区別する（subscription-state.js は
 *   'canceled' を 'inactive' に分類する）。
 *
 * ── resolveUserId の優先順位 ─────────────────────────────────────────────
 *   1. Subscription metadata.supabase_user_id（subscription_data.metadata で設定）
 *   2. subscriptions テーブル（stripe_customer_id 照合）
 *   3. Stripe Customer metadata.supabase_user_id（API 呼び出しフォールバック）
 */

import Stripe from 'npm:stripe@17.7.0';
import { SupabaseClient } from 'npm:@supabase/supabase-js@2';
import { jsonOk, jsonError } from '../_shared/response.ts';
import { getStripe } from '../_shared/stripe.ts';
import { getSupabaseAdmin } from '../_shared/supabase.ts';

Deno.serve(async (req: Request): Promise<Response> => {
  // Webhook は Stripe サーバーから呼ばれるため OPTIONS / CORS は不要
  if (req.method !== 'POST') return jsonError(405, 'Method not allowed');

  // ── 1. Stripe 署名検証 ───────────────────────────────────────────────────
  const signature = req.headers.get('stripe-signature');
  if (!signature) return jsonError(400, 'Missing stripe-signature');

  const webhookSecret = Deno.env.get('STRIPE_WEBHOOK_SECRET');
  if (!webhookSecret) return jsonError(500, 'Webhook not configured');

  let stripe: Stripe;
  try {
    stripe = getStripe();
  } catch (_) {
    return jsonError(500, 'Payment service not configured');
  }

  const body = await req.text();

  let event: Stripe.Event;
  try {
    event = stripe.webhooks.constructEvent(body, signature, webhookSecret);
  } catch (_) {
    return jsonError(400, 'Invalid signature');
  }

  // ── 2. 冪等性チェック ─────────────────────────────────────────────────────
  const supabaseAdmin = getSupabaseAdmin();

  const { data: existing } = await supabaseAdmin
    .from('stripe_webhook_events')
    .select('event_id')
    .eq('event_id', event.id)
    .maybeSingle();

  if (existing) {
    return jsonOk({ received: true, skipped: true });
  }

  // ── 3. イベント処理 ──────────────────────────────────────────────────────
  try {
    await handleEvent(event, stripe, supabaseAdmin);
  } catch (err) {
    console.error(
      '[stripe-webhook] processing error',
      event.type,
      event.id,
      (err as Error).message,
    );
    return jsonError(500, 'Processing failed');
  }

  // ── 4. 処理成功後に記録 ───────────────────────────────────────────────────
  const { error: insertError } = await supabaseAdmin
    .from('stripe_webhook_events')
    .insert({ event_id: event.id, event_type: event.type });

  if (insertError) {
    if (insertError.code === '23505') {
      // PostgreSQL unique violation: 同一 event が並列処理された
      // → 処理済み相当として 200 を返し Stripe の不要な再送を防ぐ
      return jsonOk({ received: true, duplicate: true });
    }
    // それ以外の INSERT 失敗: subscriptions 同期は完了しているため 200 を返す
    // event_id の記録漏れは次回再送時の処理重複につながるが、upsert は冪等なため無害
    console.warn(
      '[stripe-webhook] event record insert failed',
      event.id,
      insertError.code,
    );
  }

  return jsonOk({ received: true });
});

// ─────────────────────────────────────────────────────────────────────────────
// イベントディスパッチ
// ─────────────────────────────────────────────────────────────────────────────

async function handleEvent(
  event: Stripe.Event,
  stripe: Stripe,
  supabase: SupabaseClient,
): Promise<void> {
  switch (event.type) {
    case 'checkout.session.completed': {
      const session = event.data.object as Stripe.Checkout.Session;
      if (session.mode === 'subscription') {
        await handleCheckoutCompleted(session, stripe, supabase);
      }
      break;
    }
    case 'customer.subscription.created':
    case 'customer.subscription.updated': {
      const eventSub = event.data.object as Stripe.Subscription;
      await handleSubscriptionUpsert(eventSub, stripe, supabase);
      break;
    }
    case 'customer.subscription.deleted': {
      const eventSub = event.data.object as Stripe.Subscription;
      await handleSubscriptionDeleted(eventSub, stripe, supabase);
      break;
    }
    case 'invoice.payment_failed':
    case 'invoice.paid': {
      const invoice = event.data.object as Stripe.Invoice;
      await handleInvoiceEvent(invoice, stripe, supabase);
      break;
    }
    default:
      break;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// ハンドラ実装
// ─────────────────────────────────────────────────────────────────────────────

/**
 * checkout.session.completed
 * client_reference_id が user_id の一次取得元。
 * Subscription を retrieve して最新状態を upsert する。
 */
async function handleCheckoutCompleted(
  session: Stripe.Checkout.Session,
  stripe: Stripe,
  supabase: SupabaseClient,
): Promise<void> {
  const userId = session.client_reference_id;
  if (!userId) throw new Error('No client_reference_id in checkout session');

  const customerId =
    typeof session.customer === 'string' ? session.customer : null;
  const subscriptionId =
    typeof session.subscription === 'string' ? session.subscription : null;
  if (!customerId || !subscriptionId) {
    throw new Error('Missing customer or subscription in checkout session');
  }

  const subscription = await stripe.subscriptions.retrieve(subscriptionId);
  await upsertSubscription(userId, customerId, subscription, supabase);
}

/**
 * customer.subscription.created / updated
 *
 * event.data.object の状態は使わず、必ず stripe.subscriptions.retrieve() で
 * Stripe 上の最新 Subscription を取得してから upsert する。
 * 古いイベントが後着しても retrieve() は現在の状態を返すため巻き戻りが起きない。
 */
async function handleSubscriptionUpsert(
  eventSub: Stripe.Subscription,
  stripe: Stripe,
  supabase: SupabaseClient,
): Promise<void> {
  // イベントオブジェクトの状態を使わず、常に最新状態を取得する
  const subscription = await stripe.subscriptions.retrieve(eventSub.id);

  const customerId = resolveCustomerId(subscription.customer);
  if (!customerId) return;

  const userId = await resolveUserId(customerId, supabase, stripe, subscription);
  if (!userId) return;

  await upsertSubscription(userId, customerId, subscription, supabase);
}

/**
 * customer.subscription.deleted
 *
 * 削除済みサブスクリプションには retrieve が不要（削除後でも取得は可能だが、
 * イベントの subscription_id で行を特定する方が安全かつシンプル）。
 * user_id + stripe_subscription_id の両方で絞り込み、再購読後の行を誤 cancel しない。
 * 行は DELETE しない（「行なし = free (未契約)」の原則を維持）。
 */
async function handleSubscriptionDeleted(
  eventSub: Stripe.Subscription,
  stripe: Stripe,
  supabase: SupabaseClient,
): Promise<void> {
  const customerId = resolveCustomerId(eventSub.customer);
  if (!customerId) return;

  // subscription metadata → DB → Customer metadata の順で user_id を解決
  const userId = await resolveUserId(customerId, supabase, stripe, eventSub);
  if (!userId) return;

  // 解約時も Customer 対応を stripe_customers に収束させる
  await upsertStripeCustomer(userId, customerId, supabase);

  await supabase
    .from('subscriptions')
    .update({ status: 'canceled', cancel_at_period_end: false })
    .eq('user_id', userId)
    .eq('stripe_subscription_id', eventSub.id) // 再購読後の行を誤 cancel しない
    .throwOnError();
}

/**
 * invoice.paid / invoice.payment_failed
 * Subscription ID 経由で retrieve し、最新状態を upsert する。
 * customer.subscription.updated でも同期されるが補完的に処理する。
 */
async function handleInvoiceEvent(
  invoice: Stripe.Invoice,
  stripe: Stripe,
  supabase: SupabaseClient,
): Promise<void> {
  const subscriptionId =
    typeof invoice.subscription === 'string' ? invoice.subscription : null;
  if (!subscriptionId) return;

  const subscription = await stripe.subscriptions.retrieve(subscriptionId);
  const customerId = resolveCustomerId(subscription.customer);
  if (!customerId) return;

  const userId = await resolveUserId(customerId, supabase, stripe, subscription);
  if (!userId) return;

  await upsertSubscription(userId, customerId, subscription, supabase);
}

// ─────────────────────────────────────────────────────────────────────────────
// stripe_customers テーブル書き込み
// ─────────────────────────────────────────────────────────────────────────────

/**
 * stripe_customers テーブルへ Customer 対応を保存する（Webhook 経由）。
 * 既存の Checkout 以前に発行された Customer や、既存契約者の収束にも使われる。
 * 失敗してもサブスクリプション同期は完了しているため warning に留める。
 */
async function upsertStripeCustomer(
  userId: string,
  customerId: string,
  supabase: SupabaseClient,
): Promise<void> {
  const { error } = await supabase
    .from('stripe_customers')
    .upsert(
      { user_id: userId, stripe_customer_id: customerId },
      { onConflict: 'user_id' },
    );
  if (error) {
    console.warn('[stripe-webhook] stripe_customers upsert failed', userId, error.code);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 共通ユーティリティ
// ─────────────────────────────────────────────────────────────────────────────

function resolveCustomerId(
  customer:
    | string
    | Stripe.Customer
    | Stripe.DeletedCustomer
    | null,
): string | null {
  if (typeof customer === 'string') return customer;
  if (customer && 'id' in customer) return customer.id;
  return null;
}

/**
 * stripe_customer_id から user_id を解決する。
 *
 * 優先順位:
 *   1. Subscription metadata.supabase_user_id
 *      （subscription_data.metadata で設定済みのため customer.subscription.created
 *       が checkout.session.completed より先着しても解決できる）
 *   2. subscriptions テーブル（stripe_customer_id 照合）
 *   3. Stripe Customer metadata.supabase_user_id（フォールバック API 呼び出し）
 */
async function resolveUserId(
  customerId: string,
  supabase: SupabaseClient,
  stripe: Stripe,
  subscription?: Stripe.Subscription,
): Promise<string | null> {
  // 1. Subscription metadata
  const fromSubMeta = subscription?.metadata?.supabase_user_id;
  if (fromSubMeta) return fromSubMeta;

  // 2. subscriptions テーブル
  const { data } = await supabase
    .from('subscriptions')
    .select('user_id')
    .eq('stripe_customer_id', customerId)
    .maybeSingle();
  if (data?.user_id) return data.user_id;

  // 3. Stripe Customer metadata
  try {
    const customer = await stripe.customers.retrieve(customerId);
    if ('deleted' in customer && customer.deleted) return null;
    return (customer as Stripe.Customer).metadata?.supabase_user_id ?? null;
  } catch (_) {
    return null;
  }
}

/**
 * subscriptions テーブルへの upsert（冪等）。
 *
 * current_period_end: Stripe.Subscription.current_period_end は
 * stripe@17.7.0 + API 2025-02-24.acacia において number（Unix秒）として
 * Subscription 直下に存在する。* 1000 で ms に変換して ISO 文字列化する。
 * Subscription に Items が複数ある場合でも single period end を使用する
 * （本サービスは単一 Price のサブスクリプションのみを扱うため）。
 */
async function upsertSubscription(
  userId: string,
  customerId: string,
  subscription: Stripe.Subscription,
  supabase: SupabaseClient,
): Promise<void> {
  // Customer 対応を stripe_customers に収束させる（Checkout 以前の既存 Customer も対象）
  await upsertStripeCustomer(userId, customerId, supabase);

  const priceId = subscription.items.data[0]?.price.id ?? null;
  const currentPeriodEnd = new Date(
    subscription.current_period_end * 1000,
  ).toISOString();

  await supabase
    .from('subscriptions')
    .upsert(
      {
        user_id: userId,
        stripe_customer_id: customerId,
        stripe_subscription_id: subscription.id,
        status: subscription.status,
        current_period_end: currentPeriodEnd,
        price_id: priceId,
        cancel_at_period_end: subscription.cancel_at_period_end,
      },
      { onConflict: 'user_id' },
    )
    .throwOnError();
}
