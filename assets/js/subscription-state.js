/**
 * subscription-state.js
 *
 * 現在の契約状態を取得する共通モジュール。
 * pricing.html・current_context.html で import して使用する。
 *
 * 責務：
 *   - セッション確認 → ログイン済みの場合だけ subscriptions を SELECT
 *   - 正規化済み状態オブジェクトを返す
 *
 * 禁止事項（このファイル内で行わないこと）：
 *   - キャッシュ
 *   - window.* への状態保存
 *   - localStorage / sessionStorage / Cookie への保存
 *   - subscriptions への書き込み（INSERT / UPDATE / DELETE / upsert / RPC）
 *   - console への session・user・rawStatus・DBエラー全文の出力
 */

import { supabase } from './supabase-client.js';

/** 有料状態（paid）に分類する status 値 */
const PAID_STATUSES     = new Set(['active', 'trialing']);

/** 要注意状態（attention）に分類する status 値 */
const ATTENTION_STATUSES = new Set(['past_due', 'unpaid', 'paused']);

/**
 * 非アクティブ状態（inactive）に分類する status 値。
 * canceled は日付に関わらず常に inactive。
 */
const INACTIVE_STATUSES = new Set(['canceled', 'incomplete', 'incomplete_expired']);

/**
 * 現在のセッションと subscriptions テーブルを参照し、正規化済み契約状態を返す。
 *
 * @returns {Promise<{
 *   state: 'unauthenticated' | 'free' | 'paid' | 'attention' | 'inactive' | 'error',
 *   rawStatus: string | null
 * }>}
 *
 * 状態の定義：
 *   unauthenticated  getSession 正常終了・session が null
 *   free             ログイン済み・subscriptions 行なし
 *   paid             status が active または trialing
 *   attention        status が past_due / unpaid / paused
 *   inactive         status が canceled / incomplete / incomplete_expired
 *   error            getSession 戻り値 error あり / getSession 例外 /
 *                    SELECT 戻り値 error あり / SELECT 例外 /
 *                    未知 status / 想定外データ
 *
 * 注意：
 *   - 返値は UI 表示用の案内分岐にのみ使用すること。
 *   - paid であっても有料コンテンツを解放してはならない。
 *   - アクセス認可は protected API（Phase D）が担う。
 */
export async function getSubscriptionState() {
  try {
    // ── 1. セッション取得 ────────────────────────────────────────────
    const {
      data: { session },
      error: sessionError,
    } = await supabase.auth.getSession();

    // getSession 戻り値に error が含まれる場合
    if (sessionError) {
      return { state: 'error', rawStatus: null };
    }

    // セッションなし → unauthenticated
    if (!session) {
      return { state: 'unauthenticated', rawStatus: null };
    }

    // ── 2. subscriptions SELECT ──────────────────────────────────────
    // status 列のみ取得（current_period_end は B4 で使用しない）
    const { data: sub, error: subscriptionError } = await supabase
      .from('subscriptions')
      .select('status')
      .eq('user_id', session.user.id)
      .maybeSingle();

    // SELECT 戻り値に error が含まれる場合
    if (subscriptionError) {
      return { state: 'error', rawStatus: null };
    }

    // ── 3. 行なし → free ────────────────────────────────────────────
    if (sub === null) {
      return { state: 'free', rawStatus: null };
    }

    // ── 4. status 値の検証と分類 ────────────────────────────────────
    const rawStatus = (typeof sub.status === 'string') ? sub.status : null;

    // status が null または想定外の型
    if (rawStatus === null) {
      return { state: 'error', rawStatus: null };
    }

    if (PAID_STATUSES.has(rawStatus)) {
      return { state: 'paid', rawStatus };
    }

    if (ATTENTION_STATUSES.has(rawStatus)) {
      return { state: 'attention', rawStatus };
    }

    if (INACTIVE_STATUSES.has(rawStatus)) {
      // canceled は日付に関わらず常に inactive
      return { state: 'inactive', rawStatus };
    }

    // 未知の status 値 → error（free へのフォールバックは行わない）
    return { state: 'error', rawStatus };

  } catch (_) {
    // getSession または SELECT が例外をスローした場合
    return { state: 'error', rawStatus: null };
  }
}
