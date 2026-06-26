/**
 * subscription.ts — サブスクリプション認可ロジック
 *
 * 設計方針:
 *   - checkSubscriptionAccess は純粋関数。DB アクセスなし・副作用なし。
 *     ユニットテストは checkSubscriptionAccess のみを対象にできる。
 *   - fetchSubscriptionRow は DB アクセス専用。SELECT 失敗は例外を投げる。
 *     DBエラーを「無料会員」として扱わない（呼び出し側が 503 へ変換する）。
 *   - getSubscriptionAccess は両者の合成便利関数。Protected API 内で使用する。
 *
 * 呼び出し側の想定フロー（Protected API）:
 *   try {
 *     const result = await getSubscriptionAccess(supabaseAdmin, user.id);
 *     if (!result.allowed) return jsonError(403, 'Subscription required', origin);
 *     // 有料処理 ...
 *   } catch (_) {
 *     return jsonError(503, 'Service temporarily unavailable', origin);
 *   }
 */

import { SupabaseClient } from 'npm:@supabase/supabase-js@2';

// ─── 型定義 ──────────────────────────────────────────────────────────────────

/** subscriptions テーブルから取得する列の型 */
export interface SubscriptionRow {
  status: string;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
}

/**
 * サブスクリプション認可の判定結果。
 *
 * allowed=true  : アクセス許可。status は 'active' | 'trialing' のいずれか。
 * allowed=false : アクセス拒否。reason で拒否理由を区別する。
 *   subscription_missing — subscriptions 行が存在しない（正常な拒否）
 *   status_not_allowed   — status が許可リスト外（正常な拒否）
 *   period_end_missing   — current_period_end が NULL（正常な拒否）
 *   period_expired       — 現在時刻以前に期間が終了している（正常な拒否）
 *
 * DB エラー・不正日時形式は Result 型に含めず、例外として伝播させる。
 */
export type SubscriptionAccessResult =
  | {
      allowed: true;
      status: 'active' | 'trialing';
      currentPeriodEnd: string;
      cancelAtPeriodEnd: boolean;
    }
  | {
      allowed: false;
      reason:
        | 'subscription_missing'
        | 'status_not_allowed'
        | 'period_end_missing'
        | 'period_expired';
      status?: string;
    };

/** 有料アクセスを許可する status の集合 */
const ALLOWED_STATUSES = new Set<string>(['active', 'trialing']);

// ─── DB アクセス ──────────────────────────────────────────────────────────────

/**
 * 指定ユーザーの subscriptions 行を service_role クライアントで取得する。
 * 行が存在しない場合は null を返す（正常系）。
 * SELECT 失敗（ネットワーク障害・権限エラー等）は例外を投げる（呼び出し側で 503 へ変換）。
 *
 * RLS をバイパスするため service_role クライアント（getSupabaseAdmin()）を渡すこと。
 * クライアントから受け取った user_id をそのまま使用しないこと（JWT 検証後の user.id を使う）。
 */
export async function fetchSubscriptionRow(
  supabaseAdmin: SupabaseClient,
  userId: string,
): Promise<SubscriptionRow | null> {
  const { data, error } = await supabaseAdmin
    .from('subscriptions')
    .select('status, current_period_end, cancel_at_period_end')
    .eq('user_id', userId)
    .maybeSingle();

  if (error) {
    throw new Error(
      `subscriptions SELECT failed: ${error.message} (code: ${error.code})`,
    );
  }

  return data as SubscriptionRow | null;
}

// ─── 純粋な判定関数（ユニットテスト対象） ─────────────────────────────────────

/**
 * subscriptions 行と現在時刻から有料アクセスの可否を判定する。
 * DB アクセスを一切行わない純粋関数。
 *
 * 正常な拒否（allowed=false）と例外（システム不整合）を明確に区別する:
 *   - NULL / 行なし / 期間切れ → allowed=false（正常な拒否）
 *   - current_period_end が解析不能な文字列 → 例外（DB データ不整合）
 *
 * cancel_at_period_end=true でも current_period_end が未来なら許可する（仕様）。
 *
 * @param row fetchSubscriptionRow の返値（行なしの場合は null）
 * @param now 現在時刻。省略時は new Date()。テスト時に固定値を注入可能。
 */
export function checkSubscriptionAccess(
  row: SubscriptionRow | null,
  now: Date = new Date(),
): SubscriptionAccessResult {
  if (row === null) {
    return { allowed: false, reason: 'subscription_missing' };
  }

  if (!ALLOWED_STATUSES.has(row.status)) {
    return { allowed: false, reason: 'status_not_allowed', status: row.status };
  }

  if (row.current_period_end === null) {
    return { allowed: false, reason: 'period_end_missing', status: row.status };
  }

  const periodEnd = new Date(row.current_period_end);
  if (isNaN(periodEnd.getTime())) {
    // DB の current_period_end が解析不能な文字列 → システムのデータ不整合
    // period_end_missing とは別扱い: 呼び出し側が 503 を返せるよう例外として伝播する
    throw new Error(
      `Invalid current_period_end value in subscriptions: "${row.current_period_end}"`,
    );
  }

  // 厳密な比較: periodEnd が now と等しい場合は期限切れとして拒否する
  if (periodEnd <= now) {
    return { allowed: false, reason: 'period_expired', status: row.status };
  }

  return {
    allowed: true,
    status: row.status as 'active' | 'trialing',
    currentPeriodEnd: row.current_period_end,
    cancelAtPeriodEnd: row.cancel_at_period_end ?? false,
  };
}

// ─── 便利関数（DB + 判定の合成） ───────────────────────────────────────────────

/**
 * fetchSubscriptionRow + checkSubscriptionAccess を一度に実行する。
 * Protected API のハンドラで直接使用できる合成便利関数。
 *
 * DB エラー・不正日時形式は例外として伝播する（呼び出し側で 503 へ変換）。
 *
 * @param supabaseAdmin service_role クライアント（getSupabaseAdmin() の返値）
 * @param userId JWT 検証済みの user.id（クライアント送信値を使用しないこと）
 * @param now 現在時刻（省略時は new Date()）
 */
export async function getSubscriptionAccess(
  supabaseAdmin: SupabaseClient,
  userId: string,
  now: Date = new Date(),
): Promise<SubscriptionAccessResult> {
  const row = await fetchSubscriptionRow(supabaseAdmin, userId);
  return checkSubscriptionAccess(row, now);
}
