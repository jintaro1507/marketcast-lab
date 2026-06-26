/**
 * subscription_test.ts — checkSubscriptionAccess のユニットテスト
 *
 * 実行コマンド:
 *   deno test supabase/functions/_shared/subscription_test.ts
 *
 * DB アクセスを伴う fetchSubscriptionRow / getSubscriptionAccess はテスト対象外。
 * 純粋関数 checkSubscriptionAccess のみを対象とし、DB 依存なしに全14ケースを網羅する。
 */

import {
  assertEquals,
  assertThrows,
} from 'jsr:@std/assert@1';
import { checkSubscriptionAccess, type SubscriptionRow } from './subscription.ts';

// ─── テスト用固定時刻 ─────────────────────────────────────────────────────────

const NOW = new Date('2026-06-26T12:00:00.000Z');
const FUTURE = '2026-12-31T23:59:59.000Z'; // NOW より後
const PAST   = '2026-01-01T00:00:00.000Z'; // NOW より前
const EXACT_NOW = NOW.toISOString();         // NOW と等しい（期限切れ境界）

function row(overrides: Partial<SubscriptionRow> = {}): SubscriptionRow {
  return {
    status: 'active',
    current_period_end: FUTURE,
    cancel_at_period_end: false,
    ...overrides,
  };
}

// ─── ケース 1–2: 許可ステータス ───────────────────────────────────────────────

Deno.test('1: active + future period_end → allowed', () => {
  const result = checkSubscriptionAccess(row({ status: 'active' }), NOW);
  assertEquals(result.allowed, true);
  if (result.allowed) {
    assertEquals(result.status, 'active');
    assertEquals(result.currentPeriodEnd, FUTURE);
    assertEquals(result.cancelAtPeriodEnd, false);
  }
});

Deno.test('2: trialing + future period_end → allowed', () => {
  const result = checkSubscriptionAccess(row({ status: 'trialing' }), NOW);
  assertEquals(result.allowed, true);
  if (result.allowed) {
    assertEquals(result.status, 'trialing');
  }
});

// ─── ケース 3: cancel_at_period_end=true でも期間内なら許可 ──────────────────

Deno.test('3: active + cancel_at_period_end=true + future → allowed', () => {
  const result = checkSubscriptionAccess(
    row({ status: 'active', cancel_at_period_end: true }),
    NOW,
  );
  assertEquals(result.allowed, true);
  if (result.allowed) {
    assertEquals(result.cancelAtPeriodEnd, true);
  }
});

// ─── ケース 4–9: 拒否ステータス（status_not_allowed） ────────────────────────

Deno.test('4: past_due + future → denied (status_not_allowed)', () => {
  const result = checkSubscriptionAccess(row({ status: 'past_due' }), NOW);
  assertEquals(result.allowed, false);
  if (!result.allowed) {
    assertEquals(result.reason, 'status_not_allowed');
    assertEquals(result.status, 'past_due');
  }
});

Deno.test('5: unpaid + future → denied (status_not_allowed)', () => {
  const result = checkSubscriptionAccess(row({ status: 'unpaid' }), NOW);
  assertEquals(result.allowed, false);
  if (!result.allowed) {
    assertEquals(result.reason, 'status_not_allowed');
    assertEquals(result.status, 'unpaid');
  }
});

Deno.test('6: paused + future → denied (status_not_allowed)', () => {
  const result = checkSubscriptionAccess(row({ status: 'paused' }), NOW);
  assertEquals(result.allowed, false);
  if (!result.allowed) assertEquals(result.reason, 'status_not_allowed');
});

Deno.test('7: canceled + future → denied (status_not_allowed)', () => {
  const result = checkSubscriptionAccess(row({ status: 'canceled' }), NOW);
  assertEquals(result.allowed, false);
  if (!result.allowed) assertEquals(result.reason, 'status_not_allowed');
});

Deno.test('8: incomplete + future → denied (status_not_allowed)', () => {
  const result = checkSubscriptionAccess(row({ status: 'incomplete' }), NOW);
  assertEquals(result.allowed, false);
  if (!result.allowed) assertEquals(result.reason, 'status_not_allowed');
});

Deno.test('9: incomplete_expired + future → denied (status_not_allowed)', () => {
  const result = checkSubscriptionAccess(row({ status: 'incomplete_expired' }), NOW);
  assertEquals(result.allowed, false);
  if (!result.allowed) assertEquals(result.reason, 'status_not_allowed');
});

// ─── ケース 10: 行なし ────────────────────────────────────────────────────────

Deno.test('10: subscription row missing → denied (subscription_missing)', () => {
  const result = checkSubscriptionAccess(null, NOW);
  assertEquals(result.allowed, false);
  if (!result.allowed) {
    assertEquals(result.reason, 'subscription_missing');
  }
});

// ─── ケース 11: current_period_end=null ──────────────────────────────────────

Deno.test('11: active + period_end=null → denied (period_end_missing)', () => {
  const result = checkSubscriptionAccess(
    row({ status: 'active', current_period_end: null }),
    NOW,
  );
  assertEquals(result.allowed, false);
  if (!result.allowed) {
    assertEquals(result.reason, 'period_end_missing');
    assertEquals(result.status, 'active');
  }
});

// ─── ケース 12: 期間切れ（PAST / EXACT_NOW） ─────────────────────────────────

Deno.test('12a: active + period_end=past → denied (period_expired)', () => {
  const result = checkSubscriptionAccess(
    row({ status: 'active', current_period_end: PAST }),
    NOW,
  );
  assertEquals(result.allowed, false);
  if (!result.allowed) {
    assertEquals(result.reason, 'period_expired');
    assertEquals(result.status, 'active');
  }
});

Deno.test('12b: active + period_end=exactly now → denied (period_expired)', () => {
  // 厳密な比較: periodEnd <= now → 期限切れ
  const result = checkSubscriptionAccess(
    row({ status: 'active', current_period_end: EXACT_NOW }),
    NOW,
  );
  assertEquals(result.allowed, false);
  if (!result.allowed) {
    assertEquals(result.reason, 'period_expired');
  }
});

// ─── ケース 13: 不正な日時形式 → 例外（システム不整合） ──────────────────────

Deno.test('13: active + invalid period_end format → throws (system inconsistency)', () => {
  assertThrows(
    () => checkSubscriptionAccess(
      row({ status: 'active', current_period_end: 'not-a-date' }),
      NOW,
    ),
    Error,
    'Invalid current_period_end value in subscriptions',
  );
});

// ─── ケース 14: DB エラーは fetchSubscriptionRow が担うため別途ドキュメント ───

// fetchSubscriptionRow は DB エラー時に Error を throw する設計。
// SupabaseClient のモックを用いたテストは Deno の統合テスト環境で行うこと:
//   deno test --allow-net supabase/functions/_shared/subscription_test.ts
//
// 以下は設計ドキュメントとして記述する:
//   const { data: null, error: { message: 'connection refused' } }
//   → fetchSubscriptionRow が throw new Error('subscriptions SELECT failed: ...')
//   → Protected API ハンドラが catch して jsonError(503, ...) を返す
