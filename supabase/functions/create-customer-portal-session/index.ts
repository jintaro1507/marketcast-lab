import Stripe from 'npm:stripe@17.7.0';
import { handleOptions } from '../_shared/cors.ts';
import { jsonOk, jsonError } from '../_shared/response.ts';
import { getStripe } from '../_shared/stripe.ts';
import { getSupabaseAdmin, getSupabaseUserClient } from '../_shared/supabase.ts';

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

  // ── 2. stripe_customer_id 取得 ───────────────────────────────────────────
  const supabaseAdmin = getSupabaseAdmin();
  const { data: sub } = await supabaseAdmin
    .from('subscriptions')
    .select('stripe_customer_id')
    .eq('user_id', user.id)
    .maybeSingle();

  if (!sub?.stripe_customer_id) {
    return jsonError(404, 'No billing account found', origin);
  }

  // ── 3. Customer Portal Session 作成 ──────────────────────────────────────
  const siteUrl = Deno.env.get('SITE_URL') ?? '';

  let stripe: Stripe;
  try {
    stripe = getStripe();
  } catch (_) {
    return jsonError(500, 'Payment service not configured', origin);
  }

  let portalSession: Stripe.BillingPortal.Session;
  try {
    portalSession = await stripe.billingPortal.sessions.create({
      customer: sub.stripe_customer_id,
      return_url: `${siteUrl}/account.html`,
    });
  } catch (_) {
    return jsonError(502, 'Failed to create portal session', origin);
  }

  return jsonOk({ url: portalSession.url }, origin);
});
