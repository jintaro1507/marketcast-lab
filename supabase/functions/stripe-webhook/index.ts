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
 *   23505 以外の INSERT 失敗は 500 を返して Stripe に再送させる。
 *
 * ── 冪等性チェック SELECT エラー ────────────────────────────────────────
 *   事前 SELECT で DB エラーが発生した場合は 500 を返して Stripe に再送させる。
 *   エラーを無視して業務処理へ進まない。
 *
 * ── 処理結果（outcome）──────────────────────────────────────────────────
 *   各イベントの処理結果を stripe_webhook_events.outcome へ記録する。
 *   applied              … 課金・顧客情報を正常反映した
 *   ignored              … 未対応イベント、対象外 mode、更新不要
 *   unresolved_user      … Stripe イベントから user_id を解決できなかった
 *   duplicate_subscription … 同一ユーザーに別の blocking Subscription が存在した
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

import Stripe from "npm:stripe@17.7.0";
import { SupabaseClient } from "npm:@supabase/supabase-js@2";
import { jsonError, jsonOk } from "../_shared/response.ts";
import { getStripe } from "../_shared/stripe.ts";
import { getSupabaseAdmin } from "../_shared/supabase.ts";

// ─── 処理結果型 ──────────────────────────────────────────────────────────────

/** stripe_webhook_events.outcome の許可値（migration の CHECK 制約と一致） */
export type WebhookOutcome =
  | "applied"
  | "ignored"
  | "unresolved_user"
  | "duplicate_subscription";

/** handleEvent の返値。stripe_webhook_events へそのまま記録する。 */
export type WebhookProcessResult = {
  outcome: WebhookOutcome;
  stripeCustomerId?: string | null;
  stripeSubscriptionId?: string | null;
};

// ─── 定数 ────────────────────────────────────────────────────────────────────

const BLOCKING_SUBSCRIPTION_STATUSES = new Set([
  "incomplete",
  "trialing",
  "active",
  "past_due",
  "unpaid",
  "paused",
]);

// ─── テスト用エクスポート：純粋関数 ──────────────────────────────────────────

/**
 * Checkout Session の mode を評価し、'subscription' 以外なら 'ignored' を返す。
 * null を返した場合は続行（caller が処理を継続する）。
 */
export function resolveCheckoutMode(
  mode: string | null | undefined,
): "ignored" | null {
  return mode === "subscription" ? null : "ignored";
}

/**
 * Invoice オブジェクトから Subscription ID を解決する。
 * subscription フィールドが文字列でない場合は null（→ ignored）。
 */
export function resolveInvoiceSubscriptionId(
  invoice: { subscription?: unknown },
): string | null {
  return typeof invoice.subscription === "string" ? invoice.subscription : null;
}

/**
 * 既存行と受信 Subscription を比較し、別の blocking Subscription が存在するかを返す。
 * true の場合は upsert を拒否して duplicate_subscription として記録する。
 */
export function checkDuplicateSubscription(
  existingSubId: string | null | undefined,
  existingStatus: string | null | undefined,
  incomingSubId: string,
): boolean {
  return Boolean(
    existingSubId &&
      existingSubId !== incomingSubId &&
      BLOCKING_SUBSCRIPTION_STATUSES.has(existingStatus ?? ""),
  );
}

// ─── テスト用エクスポート：DB 操作ヘルパー ───────────────────────────────────

/**
 * 冪等性チェック（stripe_webhook_events への SELECT）。
 * DB エラー時は 500 Response を返す。
 * 処理済みの場合は skipped 200 を返す。
 * 未処理（継続）の場合は null を返す。
 */
export async function runIdempotencyCheck(
  supabase: SupabaseClient,
  eventId: string,
  eventType: string,
): Promise<Response | null> {
  const { data: existing, error: idempotencyError } = await supabase
    .from("stripe_webhook_events")
    .select("event_id")
    .eq("event_id", eventId)
    .maybeSingle();

  if (idempotencyError) {
    console.error(
      "[stripe-webhook] idempotency check failed",
      JSON.stringify({
        identifier: "idempotency_check_failed",
        event_id: eventId,
        event_type: eventType,
        error_code: idempotencyError.code,
      }),
    );
    return jsonError(500, "Service unavailable");
  }

  if (existing) {
    return jsonOk({ received: true, skipped: true });
  }

  return null;
}

/**
 * 処理済みイベントを stripe_webhook_events へ記録し、最終 Response を返す。
 * 23505（同時配送の競合）は 200 で受理する。
 * 23505 以外の INSERT 失敗は 500 を返して Stripe に再送させる。
 */
export async function writeEventRecord(
  supabase: SupabaseClient,
  eventId: string,
  eventType: string,
  result: WebhookProcessResult,
): Promise<Response> {
  const { error: insertError } = await supabase
    .from("stripe_webhook_events")
    .insert({
      event_id: eventId,
      event_type: eventType,
      outcome: result.outcome,
      stripe_customer_id: result.stripeCustomerId ?? null,
      stripe_subscription_id: result.stripeSubscriptionId ?? null,
      error_code: null,
    });

  if (insertError) {
    if (insertError.code === "23505") {
      return jsonOk({ received: true, duplicate: true });
    }
    console.error(
      "[stripe-webhook] event record insert failed",
      JSON.stringify({
        identifier: "event_record_insert_failed",
        event_id: eventId,
        event_type: eventType,
        error_code: insertError.code,
      }),
    );
    return jsonError(500, "Event record failed");
  }

  return jsonOk({ received: true });
}

// ─── テスト用エクスポート：イベントディスパッチ ──────────────────────────────

/**
 * イベントを種別に応じてディスパッチし、処理結果を返す。
 * throw した場合は呼び出し元が 500 を返して Stripe に再送させる。
 * テスト時は stripe / supabase をモックで注入する。
 */
export async function handleEvent(
  event: Stripe.Event,
  stripe: Stripe,
  supabase: SupabaseClient,
): Promise<WebhookProcessResult> {
  switch (event.type) {
    case "checkout.session.completed": {
      const session = event.data.object as Stripe.Checkout.Session;
      if (resolveCheckoutMode(session.mode) === "ignored") {
        return { outcome: "ignored" };
      }
      return handleCheckoutCompleted(session, stripe, supabase, event.type);
    }
    case "customer.subscription.created":
    case "customer.subscription.updated": {
      const eventSub = event.data.object as Stripe.Subscription;
      return handleSubscriptionUpsert(eventSub, stripe, supabase, event.type);
    }
    case "customer.subscription.deleted": {
      const eventSub = event.data.object as Stripe.Subscription;
      return handleSubscriptionDeleted(eventSub, stripe, supabase);
    }
    case "invoice.payment_failed":
    case "invoice.paid": {
      const invoice = event.data.object as Stripe.Invoice;
      return handleInvoiceEvent(invoice, stripe, supabase, event.type);
    }
    default:
      return { outcome: "ignored" };
  }
}

// ─── メインハンドラ ───────────────────────────────────────────────────────────

if (import.meta.main) {
  Deno.serve(async (req: Request): Promise<Response> => {
    // Webhook は Stripe サーバーから呼ばれるため OPTIONS / CORS は不要
    if (req.method !== "POST") return jsonError(405, "Method not allowed");

    // ── 1. Stripe 署名検証 ──────────────────────────────────────────────────
    const signature = req.headers.get("stripe-signature");
    if (!signature) return jsonError(400, "Missing stripe-signature");

    const webhookSecret = Deno.env.get("STRIPE_WEBHOOK_SECRET");
    if (!webhookSecret) return jsonError(500, "Webhook not configured");

    // raw body を一度だけ読み取る（constructEventAsync に渡す前に一切加工しない）
    const body = await req.text();

    // ── 署名検証（Stripe クライアント初期化より先に行う） ──────────────────
    // constructEventAsync は HMAC-SHA256 の検証のみを行い HTTP 呼び出しをしない。
    // そのため Stripe API キーは使用しない。STRIPE_SECRET_KEY が未設定でも検証は可能。
    //
    // Deno Edge Runtime には Node.js の crypto.createHmac がないため、
    // 同期版 constructEvent は使用不可。
    // SubtleCrypto（Web Crypto API）を使う非同期版 + createSubtleCryptoProvider が必須。
    const cryptoProvider = Stripe.createSubtleCryptoProvider();
    const _verifyStripe = new Stripe(
      Deno.env.get("STRIPE_SECRET_KEY") ??
        "sk_test_verify_only_placeholder_no_api_calls",
      { apiVersion: "2025-02-24.acacia" as const },
    );

    let event: Stripe.Event;
    try {
      event = await _verifyStripe.webhooks.constructEventAsync(
        body,
        signature,
        webhookSecret,
        undefined, // tolerance（デフォルト300秒）
        cryptoProvider,
      );
    } catch (_) {
      return jsonError(400, "Invalid signature");
    }

    // ── Stripe クライアント初期化（署名検証後。API 呼び出しに必要） ─────────
    let stripe: Stripe;
    try {
      stripe = getStripe();
    } catch (_) {
      return jsonError(500, "Payment service not configured");
    }

    const supabaseAdmin = getSupabaseAdmin();

    // ── 2. 冪等性チェック ────────────────────────────────────────────────────
    const idempotencyResponse = await runIdempotencyCheck(
      supabaseAdmin,
      event.id,
      event.type,
    );
    if (idempotencyResponse !== null) return idempotencyResponse;

    // ── 3. イベント処理 ─────────────────────────────────────────────────────
    let result: WebhookProcessResult;
    try {
      result = await handleEvent(event, stripe, supabaseAdmin);
    } catch (err) {
      console.error(
        "[stripe-webhook] processing error",
        event.type,
        event.id,
        (err as Error).message,
      );
      return jsonError(500, "Processing failed");
    }

    // ── 4. 処理成功後に記録 ─────────────────────────────────────────────────
    return writeEventRecord(supabaseAdmin, event.id, event.type, result);
  });
}

// ─── ハンドラ実装 ────────────────────────────────────────────────────────────

/**
 * checkout.session.completed
 * client_reference_id が user_id の一次取得元。
 * Subscription を retrieve して最新状態を upsert する。
 */
async function handleCheckoutCompleted(
  session: Stripe.Checkout.Session,
  stripe: Stripe,
  supabase: SupabaseClient,
  eventType: string,
): Promise<WebhookProcessResult> {
  const userId = session.client_reference_id;
  if (!userId) throw new Error("No client_reference_id in checkout session");

  const customerId = typeof session.customer === "string"
    ? session.customer
    : null;
  const subscriptionId = typeof session.subscription === "string"
    ? session.subscription
    : null;
  if (!customerId || !subscriptionId) {
    throw new Error("Missing customer or subscription in checkout session");
  }

  const subscription = await stripe.subscriptions.retrieve(subscriptionId);
  return upsertSubscription(
    userId,
    customerId,
    subscription,
    supabase,
    eventType,
  );
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
  eventType: string,
): Promise<WebhookProcessResult> {
  // イベントオブジェクトの状態を使わず、常に最新状態を取得する
  const subscription = await stripe.subscriptions.retrieve(eventSub.id);

  const customerId = resolveCustomerId(subscription.customer);
  if (!customerId) {
    return {
      outcome: "unresolved_user",
      stripeSubscriptionId: subscription.id,
    };
  }

  const userId = await resolveUserId(
    customerId,
    supabase,
    stripe,
    subscription,
  );
  if (!userId) {
    return {
      outcome: "unresolved_user",
      stripeCustomerId: customerId,
      stripeSubscriptionId: subscription.id,
    };
  }

  return upsertSubscription(
    userId,
    customerId,
    subscription,
    supabase,
    eventType,
  );
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
): Promise<WebhookProcessResult> {
  const customerId = resolveCustomerId(eventSub.customer);
  if (!customerId) {
    return { outcome: "unresolved_user", stripeSubscriptionId: eventSub.id };
  }

  // subscription metadata → DB → Customer metadata の順で user_id を解決
  const userId = await resolveUserId(customerId, supabase, stripe, eventSub);
  if (!userId) {
    return {
      outcome: "unresolved_user",
      stripeCustomerId: customerId,
      stripeSubscriptionId: eventSub.id,
    };
  }

  // 解約時も Customer 対応を stripe_customers に収束させる
  await upsertStripeCustomer(userId, customerId, supabase);

  await supabase
    .from("subscriptions")
    .update({ status: "canceled", cancel_at_period_end: false })
    .eq("user_id", userId)
    .eq("stripe_subscription_id", eventSub.id) // 再購読後の行を誤 cancel しない
    .throwOnError();

  return {
    outcome: "applied",
    stripeCustomerId: customerId,
    stripeSubscriptionId: eventSub.id,
  };
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
  eventType: string,
): Promise<WebhookProcessResult> {
  const subscriptionId = resolveInvoiceSubscriptionId(invoice);
  if (!subscriptionId) return { outcome: "ignored" };

  const subscription = await stripe.subscriptions.retrieve(subscriptionId);
  const customerId = resolveCustomerId(subscription.customer);
  if (!customerId) {
    return { outcome: "unresolved_user", stripeSubscriptionId: subscriptionId };
  }

  const userId = await resolveUserId(
    customerId,
    supabase,
    stripe,
    subscription,
  );
  if (!userId) {
    return {
      outcome: "unresolved_user",
      stripeCustomerId: customerId,
      stripeSubscriptionId: subscriptionId,
    };
  }

  return upsertSubscription(
    userId,
    customerId,
    subscription,
    supabase,
    eventType,
  );
}

// ─── stripe_customers テーブル書き込み ───────────────────────────────────────

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
    .from("stripe_customers")
    .upsert(
      { user_id: userId, stripe_customer_id: customerId },
      { onConflict: "user_id" },
    );
  if (error) {
    console.warn(
      "[stripe-webhook] stripe_customers upsert failed",
      userId,
      error.code,
    );
  }
}

// ─── 共通ユーティリティ ──────────────────────────────────────────────────────

function resolveCustomerId(
  customer:
    | string
    | Stripe.Customer
    | Stripe.DeletedCustomer
    | null,
): string | null {
  if (typeof customer === "string") return customer;
  if (customer && "id" in customer) return customer.id;
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
    .from("subscriptions")
    .select("user_id")
    .eq("stripe_customer_id", customerId)
    .maybeSingle();
  if (data?.user_id) return data.user_id;

  // 3. Stripe Customer metadata
  try {
    const customer = await stripe.customers.retrieve(customerId);
    if ("deleted" in customer && customer.deleted) return null;
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
  eventType: string,
): Promise<WebhookProcessResult> {
  // Customer 対応を stripe_customers に収束させる（Checkout 以前の既存 Customer も対象）
  await upsertStripeCustomer(userId, customerId, supabase);

  // 異なる Subscription ID で既存の有効行を上書きしないよう事前チェック
  // A: 既存行なし → 通常作成
  // B: 既存 stripe_subscription_id === incoming → 通常更新
  // C: 既存 stripe_subscription_id !== incoming かつ既存 status が blocking → 上書き拒否
  // D: 既存 status が canceled / incomplete_expired → 新しい Subscription へ置換を許可
  const { data: existingRow } = await supabase
    .from("subscriptions")
    .select("stripe_subscription_id, status")
    .eq("user_id", userId)
    .maybeSingle();

  if (
    checkDuplicateSubscription(
      existingRow?.stripe_subscription_id,
      existingRow?.status,
      subscription.id,
    )
  ) {
    // 再送では解決しない論理競合（同一ユーザーに複数の有効 Subscription が存在）。
    // throw すると Stripe が無限再送するため、ここでは 200 で受理する。
    // duplicate_subscription として記録し、運用確認が必要な異常として扱う。
    console.error(JSON.stringify({
      identifier: "duplicate_active_subscription_detected",
      user_id: userId,
      stored_subscription_id: existingRow?.stripe_subscription_id,
      incoming_subscription_id: subscription.id,
      stored_status: existingRow?.status,
      incoming_status: subscription.status,
      event_type: eventType,
    }));
    return {
      outcome: "duplicate_subscription",
      stripeCustomerId: customerId,
      stripeSubscriptionId: subscription.id,
    };
  }

  const priceId = subscription.items.data[0]?.price.id ?? null;
  const currentPeriodEnd = new Date(
    subscription.current_period_end * 1000,
  ).toISOString();

  await supabase
    .from("subscriptions")
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
      { onConflict: "user_id" },
    )
    .throwOnError();

  return {
    outcome: "applied",
    stripeCustomerId: customerId,
    stripeSubscriptionId: subscription.id,
  };
}
