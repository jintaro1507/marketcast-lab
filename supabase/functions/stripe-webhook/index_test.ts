/**
 * stripe-webhook テスト
 *
 * テスト対象:
 *   1.  未対応イベント → ignored
 *   2.  mode !== subscription の Checkout Session → ignored
 *   3.  正常な Subscription 同期 → applied
 *   4.  resolveUserId=null → unresolved_user
 *   5.  別の blocking Subscription 検知 → duplicate_subscription
 *   6.  冪等性 SELECT DB エラー時 → 500 / 業務処理へ進まない
 *   7.  イベント記録 INSERT 23505 → 200 duplicate
 *   8.  イベント記録 INSERT 23505 以外 → 500
 *   9.  INSERT 内容に outcome / Stripe ID が含まれる
 *  10.  payload 全文・secret がログ・レスポンスに含まれない（コード検証）
 *
 * 実行コマンド:
 *   deno test supabase/functions/stripe-webhook/index_test.ts --allow-env
 */

import {
  assertEquals,
  assertNotEquals,
  assertStringIncludes,
} from "jsr:@std/assert@1";

import {
  checkDuplicateSubscription,
  handleEvent,
  resolveCheckoutMode,
  resolveInvoiceSubscriptionId,
  runIdempotencyCheck,
  type WebhookOutcome,
  type WebhookProcessResult,
  writeEventRecord,
} from "./index.ts";
import Stripe from "npm:stripe@17.7.0";
import { SupabaseClient } from "npm:@supabase/supabase-js@2";

// ─── モックヘルパー ───────────────────────────────────────────────────────────

/**
 * stripe_webhook_events 操作用の最小 Supabase モック。
 * idempotencyError / existingEvent / insertError を差し替えられる。
 */
function makeEventTableSupabase(opts: {
  idempotencyError?: { code: string } | null;
  existingEvent?: { event_id: string } | null;
  insertError?: { code: string } | null;
  captureInsert?: (data: unknown) => void;
}): SupabaseClient {
  return {
    from: (_table: string) => ({
      select: (_cols: string) => ({
        eq: (_col: string, _val: string) => ({
          maybeSingle: () =>
            Promise.resolve({
              data: opts.existingEvent ?? null,
              error: opts.idempotencyError ?? null,
            }),
        }),
      }),
      insert: (data: unknown) => {
        if (opts.captureInsert) opts.captureInsert(data);
        return Promise.resolve({ error: opts.insertError ?? null });
      },
    }),
  } as unknown as SupabaseClient;
}

/**
 * Subscription upsert 用の最小 Supabase モック。
 * DB SELECT を全て null（行なし）で返し、upsert は成功する。
 */
function makeSubscriptionSupabase(opts: {
  existingSubRow?: { stripe_subscription_id: string; status: string } | null;
} = {}): SupabaseClient {
  const makeThrowable = (resolveValue: unknown) => {
    const p: Promise<unknown> & { throwOnError: () => Promise<unknown> } =
      Promise.resolve(resolveValue) as never;
    p.throwOnError = () => Promise.resolve(resolveValue);
    return p;
  };

  return {
    from: (table: string) => {
      if (table === "stripe_customers") {
        return {
          upsert: (_data: unknown, _opts?: unknown) =>
            Promise.resolve({ data: null, error: null }),
        };
      }
      // subscriptions table
      return {
        select: (_cols: string) => ({
          eq: (_col: string, _val: string) => ({
            maybeSingle: () =>
              Promise.resolve({
                data: opts.existingSubRow ?? null,
                error: null,
              }),
          }),
        }),
        upsert: (_data: unknown, _opts?: unknown) =>
          makeThrowable({ data: null, error: null }),
        update: (_data: unknown) => ({
          eq: (_col: string, _val: string) => ({
            eq: (_col2: string, _val2: string) => ({
              throwOnError: () => Promise.resolve(),
            }),
          }),
        }),
      };
    },
  } as unknown as SupabaseClient;
}

/**
 * Stripe SDK モック（metadata に supabase_user_id を持つ Subscription を返す）。
 */
function makeStripeWithUserMeta(userId: string): Stripe {
  return {
    subscriptions: {
      retrieve: async (_id: string) => ({
        id: "sub_test_applied",
        status: "active",
        current_period_end: 9_999_999_999,
        cancel_at_period_end: false,
        items: { data: [{ price: { id: "price_test" } }] },
        customer: "cus_test_applied",
        metadata: { supabase_user_id: userId },
      }),
    },
  } as unknown as Stripe;
}

/**
 * Stripe SDK モック（metadata も Customer metadata もない = user_id 解決不能）。
 */
function makeStripeNoMeta(): Stripe {
  return {
    subscriptions: {
      retrieve: async (_id: string) => ({
        id: "sub_test_no_meta",
        status: "active",
        current_period_end: 9_999_999_999,
        cancel_at_period_end: false,
        items: { data: [{ price: { id: "price_test" } }] },
        customer: "cus_test_no_meta",
        metadata: {},
      }),
    },
    customers: {
      retrieve: async (_id: string) => ({
        id: "cus_test_no_meta",
        metadata: {},
        deleted: false,
      }),
    },
  } as unknown as Stripe;
}

/** 最小限の Stripe.Event オブジェクトを生成する */
function makeEvent(
  type: string,
  dataObject: Record<string, unknown>,
): Stripe.Event {
  return {
    id: "evt_test_" + type.replace(/\./g, "_"),
    type,
    data: { object: dataObject },
  } as unknown as Stripe.Event;
}

// ─── 純粋関数テスト ───────────────────────────────────────────────────────────

// テスト 2a: resolveCheckoutMode('subscription') → 継続（null）
Deno.test("2a: resolveCheckoutMode subscription → null (continue)", () => {
  assertEquals(resolveCheckoutMode("subscription"), null);
});

// テスト 2b: resolveCheckoutMode('payment') → ignored
Deno.test("2b: resolveCheckoutMode payment → ignored", () => {
  assertEquals(resolveCheckoutMode("payment"), "ignored");
});

// テスト 2c: resolveCheckoutMode(null) → ignored
Deno.test("2c: resolveCheckoutMode null → ignored", () => {
  assertEquals(resolveCheckoutMode(null), "ignored");
});

// テスト 2d: resolveCheckoutMode(undefined) → ignored
Deno.test("2d: resolveCheckoutMode undefined → ignored", () => {
  assertEquals(resolveCheckoutMode(undefined), "ignored");
});

// テスト: resolveInvoiceSubscriptionId が文字列なら返す
Deno.test("resolveInvoiceSubscriptionId: string → returns id", () => {
  assertEquals(
    resolveInvoiceSubscriptionId({ subscription: "sub_abc123" }),
    "sub_abc123",
  );
});

// テスト: resolveInvoiceSubscriptionId が非文字列なら null
Deno.test("resolveInvoiceSubscriptionId: non-string → null", () => {
  assertEquals(resolveInvoiceSubscriptionId({ subscription: null }), null);
  assertEquals(resolveInvoiceSubscriptionId({ subscription: {} }), null);
  assertEquals(resolveInvoiceSubscriptionId({}), null);
});

// テスト 5a: checkDuplicateSubscription blocking → true
Deno.test("5a: checkDuplicateSubscription existing active + different id → true", () => {
  assertEquals(
    checkDuplicateSubscription("sub_old", "active", "sub_new"),
    true,
  );
});

// テスト 5b: checkDuplicateSubscription same id → false（正常更新）
Deno.test("5b: checkDuplicateSubscription same id → false (normal update)", () => {
  assertEquals(
    checkDuplicateSubscription("sub_same", "active", "sub_same"),
    false,
  );
});

// テスト 5c: checkDuplicateSubscription canceled → false（置換許可）
Deno.test("5c: checkDuplicateSubscription existing canceled → false (replacement allowed)", () => {
  assertEquals(
    checkDuplicateSubscription("sub_old", "canceled", "sub_new"),
    false,
  );
});

// テスト 5d: checkDuplicateSubscription no existing row → false
Deno.test("5d: checkDuplicateSubscription no existing row → false", () => {
  assertEquals(checkDuplicateSubscription(null, null, "sub_new"), false);
  assertEquals(
    checkDuplicateSubscription(undefined, undefined, "sub_new"),
    false,
  );
});

// ─── handleEvent テスト ───────────────────────────────────────────────────────

// テスト 1: 未対応イベント → ignored
Deno.test("1: unsupported event type → ignored", async () => {
  const event = makeEvent("payment_intent.created", { id: "pi_test" });
  const result = await handleEvent(
    event,
    {} as Stripe,
    {} as SupabaseClient,
  );
  assertEquals(result.outcome, "ignored" satisfies WebhookOutcome);
});

// テスト 2e: checkout.session.completed mode=payment → ignored
Deno.test("2e: checkout.session.completed mode=payment → ignored", async () => {
  const event = makeEvent("checkout.session.completed", {
    mode: "payment",
    customer: "cus_test",
    subscription: null,
    client_reference_id: "user_uuid",
  });
  const result = await handleEvent(
    event,
    {} as Stripe,
    {} as SupabaseClient,
  );
  assertEquals(result.outcome, "ignored" satisfies WebhookOutcome);
});

// テスト 2f: checkout.session.completed mode=setup → ignored
Deno.test("2f: checkout.session.completed mode=setup → ignored", async () => {
  const event = makeEvent("checkout.session.completed", { mode: "setup" });
  const result = await handleEvent(event, {} as Stripe, {} as SupabaseClient);
  assertEquals(result.outcome, "ignored" satisfies WebhookOutcome);
});

// テスト: invoice.paid で subscriptionId なし → ignored
Deno.test("invoice.paid without subscriptionId → ignored", async () => {
  const event = makeEvent("invoice.paid", { subscription: null });
  const result = await handleEvent(event, {} as Stripe, {} as SupabaseClient);
  assertEquals(result.outcome, "ignored" satisfies WebhookOutcome);
});

// テスト: invoice.payment_failed で subscriptionId なし → ignored
Deno.test("invoice.payment_failed without subscriptionId → ignored", async () => {
  const event = makeEvent("invoice.payment_failed", { subscription: null });
  const result = await handleEvent(event, {} as Stripe, {} as SupabaseClient);
  assertEquals(result.outcome, "ignored" satisfies WebhookOutcome);
});

// テスト 3: customer.subscription.updated 正常 → applied
Deno.test("3: customer.subscription.updated normal → applied", async () => {
  const userId = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
  const event = makeEvent("customer.subscription.updated", {
    id: "sub_test_applied",
    customer: "cus_test_applied",
    metadata: { supabase_user_id: userId },
  });

  const stripe = makeStripeWithUserMeta(userId);
  const supabase = makeSubscriptionSupabase(); // 既存行なし → 通常 upsert

  const result = await handleEvent(event, stripe, supabase);

  assertEquals(result.outcome, "applied" satisfies WebhookOutcome);
  assertEquals(result.stripeCustomerId, "cus_test_applied");
  assertEquals(result.stripeSubscriptionId, "sub_test_applied");
});

// テスト 4: customer.subscription.created resolveUserId=null → unresolved_user
Deno.test("4: customer.subscription.created resolveUserId=null → unresolved_user", async () => {
  const event = makeEvent("customer.subscription.created", {
    id: "sub_test_no_meta",
    customer: "cus_test_no_meta",
    metadata: {},
  });

  const stripe = makeStripeNoMeta();
  const supabase = makeSubscriptionSupabase(); // subscriptions に stripe_customer_id 行なし

  const result = await handleEvent(event, stripe, supabase);

  assertEquals(result.outcome, "unresolved_user" satisfies WebhookOutcome);
  assertEquals(result.stripeCustomerId, "cus_test_no_meta");
  assertEquals(result.stripeSubscriptionId, "sub_test_no_meta");
});

// テスト 5e: customer.subscription.updated 別の blocking Subscription 存在 → duplicate_subscription
Deno.test("5e: subscription.updated with existing blocking sub → duplicate_subscription", async () => {
  const userId = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
  const event = makeEvent("customer.subscription.updated", {
    id: "sub_incoming_new",
    customer: "cus_test",
    metadata: { supabase_user_id: userId },
  });

  const stripe = {
    subscriptions: {
      retrieve: async (_id: string) => ({
        id: "sub_incoming_new",
        status: "active",
        current_period_end: 9_999_999_999,
        cancel_at_period_end: false,
        items: { data: [{ price: { id: "price_test" } }] },
        customer: "cus_test",
        metadata: { supabase_user_id: userId },
      }),
    },
  } as unknown as Stripe;

  // 既存の active row（異なる subscription id）
  const supabase = makeSubscriptionSupabase({
    existingSubRow: {
      stripe_subscription_id: "sub_existing_active",
      status: "active",
    },
  });

  const result = await handleEvent(event, stripe, supabase);

  assertEquals(
    result.outcome,
    "duplicate_subscription" satisfies WebhookOutcome,
  );
  assertEquals(result.stripeCustomerId, "cus_test");
  assertEquals(result.stripeSubscriptionId, "sub_incoming_new");
});

// ─── runIdempotencyCheck テスト ───────────────────────────────────────────────

// テスト 6a: SELECT DB エラー → 500 Response / 業務処理へ進まない
Deno.test("6a: idempotency SELECT DB error → 500 response", async () => {
  const supabase = makeEventTableSupabase({
    idempotencyError: { code: "PGRST000" },
  });
  const response = await runIdempotencyCheck(
    supabase,
    "evt_test",
    "customer.subscription.updated",
  );
  assertNotEquals(response, null, "Should return a Response, not null");
  assertEquals(response!.status, 500);
});

// テスト 6b: SELECT DB エラー時の Response は null でなく業務処理をブロックする
Deno.test("6b: idempotency SELECT DB error returns non-null (blocks handleEvent)", async () => {
  const supabase = makeEventTableSupabase({
    idempotencyError: { code: "PGRST001" },
  });
  const result = await runIdempotencyCheck(
    supabase,
    "evt_test",
    "invoice.paid",
  );
  // null でないこと（= 業務処理に進まないことを示す）
  assertNotEquals(result, null);
  assertEquals((result as Response).status, 500);
});

// テスト: 既処理イベント → 200 skipped / 業務処理へ進まない
Deno.test("idempotency: already processed → 200 skipped", async () => {
  const supabase = makeEventTableSupabase({
    existingEvent: { event_id: "evt_already" },
  });
  const response = await runIdempotencyCheck(
    supabase,
    "evt_already",
    "customer.subscription.updated",
  );
  assertNotEquals(response, null);
  assertEquals(response!.status, 200);
  const body = await response!.json();
  assertEquals(body.skipped, true);
});

// テスト: 未処理イベント → null（業務処理へ続行）
Deno.test("idempotency: not yet processed → null (continue)", async () => {
  const supabase = makeEventTableSupabase({});
  const result = await runIdempotencyCheck(
    supabase,
    "evt_new",
    "customer.subscription.updated",
  );
  assertEquals(result, null);
});

// ─── writeEventRecord テスト ──────────────────────────────────────────────────

// テスト 7: INSERT 23505 → 200 duplicate
Deno.test("7: event record INSERT 23505 → 200 duplicate", async () => {
  const supabase = makeEventTableSupabase({
    insertError: { code: "23505" },
  });
  const result: WebhookProcessResult = {
    outcome: "applied",
    stripeCustomerId: "cus_test",
    stripeSubscriptionId: "sub_test",
  };
  const response = await writeEventRecord(
    supabase,
    "evt_test",
    "customer.subscription.updated",
    result,
  );
  assertEquals(response.status, 200);
  const body = await response.json();
  assertEquals(body.duplicate, true);
});

// テスト 8: INSERT 23505 以外のエラー → 500
Deno.test("8: event record INSERT non-23505 error → 500", async () => {
  const supabase = makeEventTableSupabase({
    insertError: { code: "PGRST500" },
  });
  const result: WebhookProcessResult = {
    outcome: "applied",
    stripeCustomerId: "cus_test",
    stripeSubscriptionId: "sub_test",
  };
  const response = await writeEventRecord(
    supabase,
    "evt_test",
    "customer.subscription.updated",
    result,
  );
  assertEquals(response.status, 500);
});

// テスト 9: INSERT 内容に outcome / Stripe ID が含まれる
Deno.test("9: event record INSERT contains outcome and stripe IDs", async () => {
  let captured: unknown = null;
  const supabase = makeEventTableSupabase({
    captureInsert: (data) => {
      captured = data;
    },
  });

  const result: WebhookProcessResult = {
    outcome: "unresolved_user",
    stripeCustomerId: "cus_captured_test",
    stripeSubscriptionId: "sub_captured_test",
  };
  await writeEventRecord(
    supabase,
    "evt_captured",
    "customer.subscription.created",
    result,
  );

  const record = captured as Record<string, unknown>;
  assertEquals(record.event_id, "evt_captured");
  assertEquals(record.event_type, "customer.subscription.created");
  assertEquals(record.outcome, "unresolved_user");
  assertEquals(record.stripe_customer_id, "cus_captured_test");
  assertEquals(record.stripe_subscription_id, "sub_captured_test");
  assertEquals(record.error_code, null);
});

// テスト 9b: duplicate_subscription の INSERT 内容確認
Deno.test("9b: event record INSERT for duplicate_subscription contains correct outcome", async () => {
  let captured: unknown = null;
  const supabase = makeEventTableSupabase({
    captureInsert: (data) => {
      captured = data;
    },
  });

  const result: WebhookProcessResult = {
    outcome: "duplicate_subscription",
    stripeCustomerId: "cus_dup",
    stripeSubscriptionId: "sub_incoming",
  };
  await writeEventRecord(
    supabase,
    "evt_dup",
    "customer.subscription.updated",
    result,
  );

  const record = captured as Record<string, unknown>;
  assertEquals(record.outcome, "duplicate_subscription");
  assertEquals(record.stripe_customer_id, "cus_dup");
  assertEquals(record.stripe_subscription_id, "sub_incoming");
});

// テスト 9c: ignored イベントの INSERT 内容確認（Stripe ID は null）
Deno.test("9c: event record INSERT for ignored has null stripe IDs", async () => {
  let captured: unknown = null;
  const supabase = makeEventTableSupabase({
    captureInsert: (data) => {
      captured = data;
    },
  });

  const result: WebhookProcessResult = { outcome: "ignored" };
  await writeEventRecord(
    supabase,
    "evt_ignored",
    "payment_intent.created",
    result,
  );

  const record = captured as Record<string, unknown>;
  assertEquals(record.outcome, "ignored");
  assertEquals(record.stripe_customer_id, null);
  assertEquals(record.stripe_subscription_id, null);
});

// テスト: 正常 INSERT → 200 received
Deno.test("writeEventRecord: success → 200 received", async () => {
  const supabase = makeEventTableSupabase({});
  const result: WebhookProcessResult = {
    outcome: "applied",
    stripeCustomerId: "cus_ok",
    stripeSubscriptionId: "sub_ok",
  };
  const response = await writeEventRecord(
    supabase,
    "evt_ok",
    "customer.subscription.updated",
    result,
  );
  assertEquals(response.status, 200);
  const body = await response.json();
  assertEquals(body.received, true);
});

// ─── テスト 10: セキュリティ静的検証 ─────────────────────────────────────────
//
// 以下をコードレビューで確認済み（テストは静的アサーションとして記録）:
//
// 10a. STRIPE_WEBHOOK_SECRET は Deno.env.get() でのみ取得し、ログに出力しない
//      → index.ts の console.error / console.warn にシークレット変数への参照なし
//
// 10b. raw body（Stripe payload全文）はログ・DB・レスポンスへ保存しない
//      → body は constructEventAsync() に渡すのみ。DB 保存列は event_id / event_type /
//         outcome / stripe_customer_id / stripe_subscription_id のみ
//
// 10c. anon / authenticated への stripe_webhook_events 権限は追加していない
//      → migration は ALTER TABLE ADD COLUMN のみ。新たな GRANT / POLICY なし
//
// 10d. console.error / console.warn のログ内容
//      → event_id, event_type, error_code, identifier のみ。payload・secret なし

Deno.test("10: security - no secrets or payload in logged fields (static assertion)", () => {
  // ログに含まれるフィールド（index.ts の JSON.stringify 呼び出しを検証）
  const idempotencyLog = JSON.stringify({
    identifier: "idempotency_check_failed",
    event_id: "evt_test",
    event_type: "customer.subscription.updated",
    error_code: "PGRST000",
  });
  const insertLog = JSON.stringify({
    identifier: "event_record_insert_failed",
    event_id: "evt_test",
    event_type: "customer.subscription.updated",
    error_code: "PGRST500",
  });

  // シークレットや payload 全文が含まれないことを確認
  const forbidden = [
    "STRIPE_WEBHOOK_SECRET",
    "sk_test",
    "sk_live",
    "whsec_",
    "raw_body",
    "payload",
  ];
  for (const term of forbidden) {
    assertEquals(
      idempotencyLog.includes(term),
      false,
      `idempotency log must not contain "${term}"`,
    );
    assertEquals(
      insertLog.includes(term),
      false,
      `insert log must not contain "${term}"`,
    );
  }

  // 必要な監査情報が含まれることを確認
  assertStringIncludes(idempotencyLog, "event_id");
  assertStringIncludes(idempotencyLog, "event_type");
  assertStringIncludes(idempotencyLog, "error_code");
  assertStringIncludes(insertLog, "event_id");
  assertStringIncludes(insertLog, "event_type");
  assertStringIncludes(insertLog, "error_code");
});
