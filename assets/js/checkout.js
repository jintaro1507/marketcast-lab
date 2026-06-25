/**
 * checkout.js
 *
 * Stripe Checkout / Customer Portal への遷移を担うモジュール。
 *
 * セキュリティ注記：
 *   - supabase.functions.invoke() が JWT を自動付与するため、
 *     Secret 情報やトークンをブラウザ側で保持・操作しない。
 *   - レスポンスは URL のみ。Stripe Secret Key は Edge Function 側にのみ存在する。
 */

import { supabase } from './supabase-client.js';

/**
 * Stripe Checkout Session を作成してリダイレクトする。
 * @param {HTMLButtonElement} btn - ローディング状態を管理するボタン
 * @param {HTMLElement|null} [errorEl] - エラー表示要素（省略可）
 */
export async function startCheckout(btn, errorEl = null) {
  if (btn.disabled) return;
  _setLoading(btn, true);
  _hideError(errorEl);

  try {
    const { data, error } = await supabase.functions.invoke(
      'create-checkout-session',
    );

    if (error) {
      const status = error?.context?.status ?? 0;
      if (status === 409) {
        _showError(
          errorEl,
          'すでに有料プランをご利用中です。アカウントページからご確認ください。',
        );
      } else if (status === 401) {
        // 認証切れ：ログインページへ誘導
        window.location.href = 'login.html';
      } else {
        _showError(errorEl, '処理に失敗しました。しばらくしてから再度お試しください。');
      }
      return;
    }

    if (data?.url) {
      window.location.href = data.url;
    } else {
      _showError(errorEl, '処理に失敗しました。しばらくしてから再度お試しください。');
    }
  } catch (_) {
    _showError(errorEl, 'ネットワークエラーが発生しました。しばらくしてから再度お試しください。');
  } finally {
    _setLoading(btn, false);
  }
}

/**
 * Stripe Customer Portal Session を作成してリダイレクトする。
 * @param {HTMLButtonElement} btn
 * @param {HTMLElement|null} [errorEl]
 */
export async function openCustomerPortal(btn, errorEl = null) {
  if (btn.disabled) return;
  _setLoading(btn, true);
  _hideError(errorEl);

  try {
    const { data, error } = await supabase.functions.invoke(
      'create-customer-portal-session',
    );

    if (error) {
      const status = error?.context?.status ?? 0;
      if (status === 401) {
        window.location.href = 'login.html';
      } else if (status === 404) {
        // stripe_customer_id が存在しない（通常は起きない）
        _showError(
          errorEl,
          'お支払い情報が見つかりません。サポートにお問い合わせください。',
        );
      } else {
        _showError(errorEl, '処理に失敗しました。しばらくしてから再度お試しください。');
      }
      return;
    }

    if (data?.url) {
      window.location.href = data.url;
    } else {
      _showError(errorEl, '処理に失敗しました。しばらくしてから再度お試しください。');
    }
  } catch (_) {
    _showError(errorEl, 'ネットワークエラーが発生しました。しばらくしてから再度お試しください。');
  } finally {
    _setLoading(btn, false);
  }
}

// ── プライベートヘルパー ──────────────────────────────────────────────────

function _setLoading(btn, loading) {
  if (!btn) return;
  btn.disabled = loading;
  btn.dataset.originalText = btn.dataset.originalText || btn.textContent;
  btn.textContent = loading ? '処理中…' : btn.dataset.originalText;
}

function _showError(el, msg) {
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
}

function _hideError(el) {
  if (!el) return;
  el.hidden = true;
}
