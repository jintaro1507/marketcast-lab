import { supabase } from './supabase-client.js';

/**
 * [data-auth-link] 要素を認証状態に応じて切り替える
 * - 未ログイン: テキスト「ログイン」、href="login.html"
 * - ログイン済み: テキスト「アカウント」、href="account.html"
 */
function updateAuthLinks(session) {
  const links = document.querySelectorAll('[data-auth-link]');
  links.forEach(link => {
    if (session) {
      link.textContent = 'アカウント';
      link.href = 'account.html';
    } else {
      link.textContent = 'ログイン';
      link.href = 'login.html';
    }
  });
}

(async function () {
  // 初期セッション確認
  try {
    const { data } = await supabase.auth.getSession();
    updateAuthLinks(data.session);
  } catch (_) {
    updateAuthLinks(null);
  }

  // セッション変化を監視してナビゲーションをリアクティブに更新
  const {
    data: { subscription },
  } = supabase.auth.onAuthStateChange((_event, session) => {
    updateAuthLinks(session);
  });

  // ページ離脱時に購読解除
  window.addEventListener('pagehide', () => {
    subscription.unsubscribe();
  }, { once: true });
})();
