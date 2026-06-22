import { supabase } from './supabase-client.js';

// ベースURL（emailRedirectTo / redirectTo 用）
const BASE_URL = 'https://jintaro1507.github.io/marketcast-lab/';

// ======================================================
// エラーメッセージ変換
// ======================================================
const ERROR_MAP = [
  // ログイン認証失敗
  { code: 'invalid_credentials',
    pattern: /invalid login credentials/i,
    msg: 'メールアドレスまたはパスワードが正しくありません。' },
  // メール未確認
  { code: 'email_not_confirmed',
    pattern: /email not confirmed/i,
    msg: 'メールアドレスの確認が完了していません。確認メールをご確認ください。' },
  // メール形式不正
  { code: 'invalid_email',
    pattern: /invalid email|unable to validate email/i,
    msg: '有効なメールアドレスを入力してください。' },
  // 弱いパスワード
  { code: 'weak_password',
    pattern: /weak password|password should be at least/i,
    msg: 'パスワードは8文字以上で入力してください。' },
  // レート制限
  { code: 'over_email_send_rate_limit',
    pattern: /rate limit|too many requests|email rate limit exceeded|over.*email.*limit|for security purposes.*only.*request/i,
    msg: 'しばらく時間をおいてから再度お試しください。' },
  // セッション不在
  { code: 'session_not_found',
    pattern: /auth session missing/i,
    msg: 'セッションが見つかりません。再度ログインしてください。' },
];

// 既存ユーザー登録エラーのパターン（signup 専用: 成功案内へ変換）
const ALREADY_REGISTERED_PATTERNS = [
  /user already registered/i,
  /email already in use/i,
  /already been registered/i,
];
const ALREADY_REGISTERED_CODES = ['user_already_exists', 'email_exists'];

function isAlreadyRegisteredError(err) {
  if (!err) return false;
  if (err.code && ALREADY_REGISTERED_CODES.includes(err.code)) return true;
  const raw = (err.message || '').toLowerCase();
  return ALREADY_REGISTERED_PATTERNS.some(p => p.test(raw));
}

function toJaMsg(err) {
  if (!err) return 'エラーが発生しました。時間をおいて再度お試しください。';
  // code が存在する場合は優先して判定
  if (err.code) {
    const byCode = ERROR_MAP.find(e => e.code === err.code);
    if (byCode) return byCode.msg;
  }
  // pattern フォールバック
  const raw = (err.message || err.msg || String(err)).toLowerCase();
  for (const { pattern, msg } of ERROR_MAP) {
    if (pattern.test(raw)) return msg;
  }
  return 'エラーが発生しました。時間をおいて再度お試しください。';
}

// ======================================================
// UI ヘルパー
// ======================================================
function showMsg(el, text, isError) {
  if (!el) return;
  el.textContent = text;
  el.className = isError ? 'auth-msg auth-msg--error' : 'auth-msg auth-msg--ok';
  el.hidden = false;
}

function setLoading(btn, loading) {
  if (!btn) return;
  btn.disabled = loading;
  btn.dataset.originalText = btn.dataset.originalText || btn.textContent;
  btn.textContent = loading ? '処理中…' : btn.dataset.originalText;
}

// メールアドレス形式検証（ブラウザの validity を利用）
function isEmailInvalid(inputEl) {
  return inputEl.validity.typeMismatch || inputEl.validity.valueMissing;
}

// ======================================================
// ページ: login
// ======================================================
function initLogin() {
  const msgEl = document.getElementById('auth-message');
  const form  = document.getElementById('login-form');

  // ログイン済みなら account.html へ固定遷移
  (async () => {
    try {
      const { data } = await supabase.auth.getSession();
      if (data.session) {
        window.location.href = 'account.html';
        return;
      }
    } catch (_) { /* セッション取得失敗は無視してフォーム表示継続 */ }

    // ?confirmed=1 の確認メール完了メッセージ
    const params = new URLSearchParams(window.location.search);
    if (params.get('confirmed') === '1') {
      showMsg(msgEl, 'メールアドレスの確認が完了しました。ログインしてください。', false);
    }
  })();

  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const emailInput = form.querySelector('[name="email"]');
    const email      = emailInput.value.trim();
    const password   = form.querySelector('[name="password"]').value;
    const btn        = form.querySelector('[type="submit"]');

    // クライアント検証
    if (!email || !password) {
      showMsg(msgEl, 'メールアドレスとパスワードを入力してください。', true);
      return;
    }
    if (isEmailInvalid(emailInput)) {
      showMsg(msgEl, '有効なメールアドレスを入力してください。', true);
      return;
    }

    setLoading(btn, true);
    msgEl && (msgEl.hidden = true);

    try {
      const { error } = await supabase.auth.signInWithPassword({ email, password });
      if (error) {
        showMsg(msgEl, toJaMsg(error), true);
        return;
      }
      window.location.href = 'index.html';
    } catch (_) {
      showMsg(msgEl, 'エラーが発生しました。時間をおいて再度お試しください。', true);
    } finally {
      setLoading(btn, false);
    }
  });
}

// ======================================================
// ページ: signup
// ======================================================
function initSignup() {
  const form  = document.getElementById('signup-form');
  const msgEl = document.getElementById('auth-message');

  // ログイン済みなら account.html へ固定遷移
  (async () => {
    try {
      const { data } = await supabase.auth.getSession();
      if (data.session) {
        window.location.href = 'account.html';
        return;
      }
    } catch (_) { /* セッション取得失敗は無視してフォーム表示継続 */ }
  })();

  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const emailInput   = form.querySelector('[name="email"]');
    const email        = emailInput.value.trim();
    const password     = form.querySelector('[name="password"]').value;
    const confirm      = form.querySelector('[name="confirm-password"]').value;
    const btn          = form.querySelector('[type="submit"]');

    // クライアント検証
    if (!email || !password || !confirm) {
      showMsg(msgEl, 'すべての項目を入力してください。', true);
      return;
    }
    if (isEmailInvalid(emailInput)) {
      showMsg(msgEl, '有効なメールアドレスを入力してください。', true);
      return;
    }
    if (password.length < 8) {
      showMsg(msgEl, 'パスワードは8文字以上で入力してください。', true);
      return;
    }
    if (password !== confirm) {
      showMsg(msgEl, 'パスワードが一致しません。', true);
      return;
    }

    setLoading(btn, true);
    msgEl && (msgEl.hidden = true);

    try {
      const { error } = await supabase.auth.signUp({
        email,
        password,
        options: {
          emailRedirectTo: BASE_URL + 'login.html?confirmed=1',
        },
      });

      if (error) {
        // 既存ユーザー登録エラーのみ成功案内へ変換（ユーザー列挙防止）
        if (isAlreadyRegisteredError(error)) {
          form.hidden = true;
          showMsg(msgEl,
            '登録可能な場合は確認メールを送信しました。メールをご確認ください。',
            false
          );
          return;
        }
        // それ以外のエラー（通信障害・Rate Limit・無効メール・弱いパスワード等）
        showMsg(msgEl, toJaMsg(error), true);
        return;
      }

      // 正常登録成功
      form.hidden = true;
      showMsg(msgEl,
        '登録可能な場合は確認メールを送信しました。メールをご確認ください。',
        false
      );
    } catch (_) {
      showMsg(msgEl, 'エラーが発生しました。時間をおいて再度お試しください。', true);
    } finally {
      setLoading(btn, false);
    }
  });
}

// ======================================================
// ページ: forgot-password
// ======================================================
function initForgotPassword() {
  const form  = document.getElementById('forgot-form');
  const msgEl = document.getElementById('auth-message');
  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const emailInput = form.querySelector('[name="email"]');
    const email      = emailInput.value.trim();
    const btn        = form.querySelector('[type="submit"]');

    // クライアント検証
    if (!email) {
      showMsg(msgEl, 'メールアドレスを入力してください。', true);
      return;
    }
    if (isEmailInvalid(emailInput)) {
      showMsg(msgEl, '有効なメールアドレスを入力してください。', true);
      return;
    }

    setLoading(btn, true);
    msgEl && (msgEl.hidden = true);

    try {
      const { error } = await supabase.auth.resetPasswordForEmail(email, {
        redirectTo: BASE_URL + 'reset-password.html',
      });

      if (error) {
        showMsg(msgEl, toJaMsg(error), true);
        return;
      }

      // 成功時: メールアドレス存在有無にかかわらず同一メッセージ
      form.hidden = true;
      showMsg(msgEl,
        '登録済みのメールアドレスの場合は、再設定メールを送信しました。',
        false
      );
    } catch (_) {
      showMsg(msgEl, 'エラーが発生しました。時間をおいて再度お試しください。', true);
    } finally {
      setLoading(btn, false);
    }
  });
}

// ======================================================
// ページ: reset-password
// ======================================================
function initResetPassword() {
  const formWrapper  = document.getElementById('reset-form-wrapper');
  const form         = document.getElementById('reset-form');
  const msgEl        = document.getElementById('auth-message');
  const checkingEl   = document.getElementById('reset-checking');
  const invalidEl    = document.getElementById('reset-invalid');

  // --- 状態管理（loading / invalid / ready の排他制御）---
  function setResetState(state) {
    if (checkingEl)  checkingEl.hidden  = (state !== 'loading');
    if (invalidEl)   invalidEl.hidden   = (state !== 'invalid');
    if (formWrapper) formWrapper.hidden = (state !== 'ready');
  }

  // 回復セッション確認フラグ
  let recoveryReady = false;

  // 初期状態: loading
  setResetState('loading');

  // タイマーを先に作成することで、PASSWORD_RECOVERY が購読登録直後に発火しても
  // clearTimeout(timerId) が有効な値を参照できる
  let timerId = setTimeout(() => {
    timerId = null;
    recoveryReady = false;
    setResetState('invalid');
  }, 10000);

  // PASSWORD_RECOVERY を onAuthStateChange で購読
  const {
    data: { subscription },
  } = supabase.auth.onAuthStateChange((event, _session) => {
    if (event === 'PASSWORD_RECOVERY') {
      clearTimeout(timerId);
      timerId = null;
      recoveryReady = true;
      setResetState('ready');
    }
  });

  // ページ離脱時: タイマーと購読の両方を解除
  window.addEventListener('pagehide', () => {
    if (timerId !== null) {
      clearTimeout(timerId);
      timerId = null;
    }
    subscription.unsubscribe();
  }, { once: true });

  if (!form) return;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();

    // 回復セッションが確認できていない場合はフォーム送信を拒否
    if (!recoveryReady) {
      showMsg(
        msgEl,
        '再設定リンクが無効または期限切れです。再度パスワード再設定を行ってください。',
        true
      );
      return;
    }

    const newPass = form.querySelector('[name="new-password"]').value;
    const confirm = form.querySelector('[name="confirm-password"]').value;
    const btn     = form.querySelector('[type="submit"]');

    if (!newPass || !confirm) {
      showMsg(msgEl, '新しいパスワードを入力してください。', true);
      return;
    }
    if (newPass.length < 8) {
      showMsg(msgEl, 'パスワードは8文字以上で入力してください。', true);
      return;
    }
    if (newPass !== confirm) {
      showMsg(msgEl, 'パスワードが一致しません。', true);
      return;
    }

    setLoading(btn, true);
    msgEl && (msgEl.hidden = true);

    try {
      const { error } = await supabase.auth.updateUser({ password: newPass });
      if (error) {
        showMsg(msgEl, toJaMsg(error), true);
        return;
      }
      form.hidden = true;
      showMsg(msgEl, 'パスワードを更新しました。', false);
      // 3秒後にログインページへ誘導
      setTimeout(() => {
        window.location.href = 'login.html';
      }, 3000);
    } catch (_) {
      showMsg(msgEl, 'エラーが発生しました。時間をおいて再度お試しください。', true);
    } finally {
      setLoading(btn, false);
    }
  });
}

// ======================================================
// ページ: account
// ======================================================
function initAccount() {
  const emailEl   = document.getElementById('account-email');
  const logoutBtn = document.getElementById('logout-btn');

  (async () => {
    try {
      const { data } = await supabase.auth.getSession();
      if (!data.session) {
        window.location.href = 'login.html';
        return;
      }
      if (emailEl) emailEl.textContent = data.session.user.email;
    } catch (_) {
      window.location.href = 'login.html';
    }
  })();

  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      setLoading(logoutBtn, true);
      try {
        await supabase.auth.signOut();
        window.location.href = 'index.html';
      } catch (_) {
        setLoading(logoutBtn, false);
      }
    });
  }
}

// ======================================================
// エントリポイント：data-auth-page で分岐
// ======================================================
const page = document.body.dataset.authPage;
switch (page) {
  case 'login':           initLogin();           break;
  case 'signup':          initSignup();          break;
  case 'forgot-password': initForgotPassword();  break;
  case 'reset-password':  initResetPassword();   break;
  case 'account':         initAccount();         break;
}
