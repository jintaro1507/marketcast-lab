/**
 * stripe.ts — Stripe クライアント初期化
 *
 * SDK:  npm:stripe@17.7.0
 * API:  2025-02-24.acacia（stripe@17.7.0 の LatestApiVersion）
 *
 * current_period_end は Stripe.Subscription.current_period_end: number（Unix秒）で
 * 取得する。stripe@17.7.0 + API 2025-02-24.acacia でこのフィールドは直接 Subscription に存在する。
 * 将来 API バージョンを上げる場合は Subscription Item 側の period.end への移行を検討すること。
 */

import Stripe from 'npm:stripe@17.7.0';

let _stripe: Stripe | null = null;

export function getStripe(): Stripe {
  if (_stripe) return _stripe;
  const key = Deno.env.get('STRIPE_SECRET_KEY');
  if (!key) throw new Error('STRIPE_SECRET_KEY not configured');
  _stripe = new Stripe(key, { apiVersion: '2025-02-24.acacia' });
  return _stripe;
}
