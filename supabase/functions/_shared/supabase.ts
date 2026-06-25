import { createClient, SupabaseClient } from 'npm:@supabase/supabase-js@2';

/**
 * service_role 権限の管理クライアント。
 * subscriptions への書き込み・stripe_webhook_events の操作に使用。
 * RLS をバイパスする。
 */
export function getSupabaseAdmin(): SupabaseClient {
  return createClient(
    Deno.env.get('SUPABASE_URL')!,
    Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!,
    { auth: { persistSession: false } },
  );
}

/**
 * ユーザーの JWT を引き継ぐ認証クライアント。
 * getUser() でトークン検証に使用し、その後の DB 操作は admin クライアントで行う。
 */
export function getSupabaseUserClient(authHeader: string): SupabaseClient {
  return createClient(
    Deno.env.get('SUPABASE_URL')!,
    Deno.env.get('SUPABASE_ANON_KEY')!,
    {
      global: { headers: { Authorization: authHeader } },
      auth: { persistSession: false },
    },
  );
}
