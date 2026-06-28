/**
 * weekly-marketcast.js
 *
 * Weekly Marketcast 有料コンテンツ取得・表示モジュール。
 * weekly_marketcast.html のインラインスクリプトから import して使用する。
 *
 * 責務:
 *   - week_id クエリパラメータの検証
 *   - Supabase セッション取得 + get-weekly-marketcast 呼び出し
 *   - API レスポンスの構造検証
 *   - XSS 安全な DOM 構築（innerHTML 不使用）
 *   - 認証状態変化時のコンテンツ消去
 *   - 二重リクエスト防止（AbortController）
 *
 * セキュリティ:
 *   - innerHTML による動的文字列挿入を行わない
 *   - JWT・session をコンソールへ出力しない
 *   - paid_body を console に出力しない
 *   - forbidden key を表示しない
 *   - end_value を表示しない
 *   - 内部列（teaser_hash, paid_body_hash）を表示しない
 *   - URL query パラメータを DOM へ直接挿入しない
 */

import { supabase } from './supabase-client.js';

/* ─── 定数 ─────────────────────────────────────────────────────────────── */

const SUPABASE_URL = 'https://lvsustmfqrxjnfgdtlna.supabase.co';
const FUNCTION_NAME = 'get-weekly-marketcast';

const WEEK_ID_RE = /^\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])$/;

/** 表示を禁止するキー（Function の FORBIDDEN_RAW_KEYS に対応） */
const FORBIDDEN_DISPLAY_KEYS = new Set([
  'value', 'current_value', 'previous_value', 'latest_value',
  'raw_value', 'price', 'close', 'api_key', 'service_role_key',
  'authorization', 'jwt',
]);

/** 正式 6 資産の表示名マップ */
const ASSET_LABELS = {
  wti:    'WTI原油',
  gold:   '金（Gold）',
  sp500:  'S&P 500',
  ust10y: '米10年国債利回り',
  usdjpy: 'USD/JPY',
  vix:    'VIX恐怖指数',
};
const VALID_ASSET_KEYS = new Set(['wti', 'gold', 'sp500', 'ust10y', 'usdjpy', 'vix']);

/** タイムライン期間ラベル */
const PERIOD_LABELS = { d1: '1日後', d7: '7日後', d30: '30日後', d90: '90日後' };
const PERIOD_KEYS = ['d1', 'd7', 'd30', 'd90'];

/** direction 表示テキスト */
const DIR_TEXT  = { up: '↑ 上昇', down: '↓ 下落', flat: '→ 横ばい', na: '—' };
const DIR_CLASS = { up: 'dir-up', down: 'dir-down', flat: 'dir-flat', na: 'dir-na' };

/* ─── 進行中リクエスト管理 ─────────────────────────────────────────────── */

let _currentAbort = null;

/* ─── week_id 検証 ─────────────────────────────────────────────────────── */

/**
 * week_id 形式検証（YYYY-Www）。
 * W53 の実在チェックは行わない（API 側で行う）。
 * @param {string} weekId
 * @returns {boolean}
 */
export function isValidWeekId(weekId) {
  return typeof weekId === 'string' && WEEK_ID_RE.test(weekId);
}

/* ─── forbidden key チェック ────────────────────────────────────────────── */

/**
 * paid_body に forbidden key が含まれないかを再帰的に検査する。
 * @param {unknown} obj
 * @returns {boolean}
 */
export function hasForbiddenKey(obj) {
  if (Array.isArray(obj)) return obj.some(hasForbiddenKey);
  if (obj !== null && typeof obj === 'object') {
    for (const [k, v] of Object.entries(obj)) {
      if (FORBIDDEN_DISPLAY_KEYS.has(k)) return true;
      if (hasForbiddenKey(v)) return true;
    }
  }
  return false;
}

/* ─── レスポンス構造検証 ────────────────────────────────────────────────── */

/**
 * 200 レスポンスの構造が期待どおりか検証する。
 * @param {unknown} data
 * @returns {boolean}
 */
export function validateWeeklyResponse(data) {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return false;

  // 必須トップレベルフィールド
  const REQUIRED = ['week_id', 'revision', 'title', 'period_start', 'period_end',
                    'published_at', 'paid_body'];
  for (const f of REQUIRED) {
    if (!(f in data)) return false;
  }

  const pb = data.paid_body;
  if (typeof pb !== 'object' || pb === null || Array.isArray(pb)) return false;

  // paid_body 必須フィールド
  const PB_REQUIRED = ['summary', 'asset_summaries', 'themes',
                       'similar_events', 'observation_points', 'disclaimer'];
  for (const f of PB_REQUIRED) {
    if (!(f in pb)) return false;
  }

  if (!Array.isArray(pb.asset_summaries)) return false;
  if (!Array.isArray(pb.themes)) return false;
  if (!Array.isArray(pb.similar_events)) return false;
  if (!Array.isArray(pb.observation_points)) return false;

  // forbidden key チェック
  if (hasForbiddenKey(pb)) return false;

  return true;
}

/* ─── API 呼び出し ──────────────────────────────────────────────────────── */

/**
 * get-weekly-marketcast を呼び出す。
 * @param {string} weekId
 * @param {string} accessToken
 * @param {AbortSignal} signal
 * @returns {Promise<{ status: number, data: unknown }>}
 */
async function fetchWeeklyReport(weekId, accessToken, signal) {
  const url = `${SUPABASE_URL}/functions/v1/${FUNCTION_NAME}?week_id=${encodeURIComponent(weekId)}`;
  const res = await fetch(url, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    signal,
  });

  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    /* parse 失敗は status だけで処理 */
  }

  return { status: res.status, data };
}

/* ─── メインエントリポイント ────────────────────────────────────────────── */

/**
 * Weekly Marketcast ページを初期化する。
 * weekly_marketcast.html のインラインスクリプトから呼ぶ。
 *
 * @param {HTMLElement} container - #weekly-app 要素
 * @param {string|null} weekId    - URL から取得した week_id（null=未指定）
 */
export async function initWeeklyPage(container, weekId) {
  if (!container) return;

  // 前回表示を即時消去
  _clearContent(container);
  _renderLoading(container, '認証状態を確認しています…');

  // 進行中リクエストをキャンセル
  if (_currentAbort) {
    _currentAbort.abort();
    _currentAbort = null;
  }

  // week_id 未指定
  if (!weekId) {
    _renderEmpty(container);
    return;
  }

  // week_id 形式不正
  if (!isValidWeekId(weekId)) {
    _renderInvalidWeekId(container);
    return;
  }

  // セッション取得
  let session;
  try {
    const { data, error } = await supabase.auth.getSession();
    if (error || !data.session) {
      _renderLoginRequired(container);
      return;
    }
    session = data.session;
  } catch (_) {
    _renderNetworkError(container);
    return;
  }

  // レポート取得
  _renderLoading(container, 'Weekly Marketcastを読み込んでいます…');

  const abort = new AbortController();
  _currentAbort = abort;

  let status, data;
  try {
    ({ status, data } = await fetchWeeklyReport(weekId, session.access_token, abort.signal));
  } catch (err) {
    if (err && err.name === 'AbortError') return;
    _renderNetworkError(container);
    return;
  } finally {
    if (_currentAbort === abort) _currentAbort = null;
  }

  // 状態別描画
  if (status === 200) {
    if (!validateWeeklyResponse(data)) {
      _renderUnexpectedError(container);
      return;
    }
    _renderReport(container, data);
  } else if (status === 401) {
    _clearContent(container);
    _renderLoginRequired(container);
  } else if (status === 403) {
    _clearContent(container);
    _renderUpgradeRequired(container);
  } else if (status === 404) {
    _renderNotPublished(container);
  } else if (status === 400) {
    _renderInvalidWeekId(container);
  } else if (status === 405) {
    _renderGenericError(container);
  } else {
    _renderNetworkError(container);
  }
}

/* ─── 認証状態変化監視 ──────────────────────────────────────────────────── */

/**
 * セッション変化を監視し、ログアウト時にコンテンツを消去する。
 * @param {HTMLElement} container
 * @returns {Function} unsubscribe 関数
 */
export function watchAuthState(container, weekId) {
  const { data: { subscription } } = supabase.auth.onAuthStateChange((event, _session) => {
    if (event === 'SIGNED_OUT' || event === 'TOKEN_REFRESHED') {
      if (_currentAbort) {
        _currentAbort.abort();
        _currentAbort = null;
      }
      _clearContent(container);
      if (event === 'SIGNED_OUT') {
        _renderLoginRequired(container);
      } else {
        // TOKEN_REFRESHED: 新しいトークンで再取得
        initWeeklyPage(container, weekId);
      }
    }
  });
  return () => subscription.unsubscribe();
}

/* ─── 描画関数 ──────────────────────────────────────────────────────────── */

function _clearContent(container) {
  container.replaceChildren();
}

function _renderLoading(container, msg) {
  const d = _el('div', 'wm-loading');
  d.setAttribute('aria-busy', 'true');
  d.setAttribute('aria-live', 'polite');
  d.appendChild(_el('span', '', msg || '読み込み中…'));
  container.replaceChildren(d);
}

function _renderEmpty(container) {
  const box = _el('div', 'wm-gate-box');
  box.appendChild(_el('p', 'wm-gate-msg',
    '現在公開されているWeekly Marketcastはありません。'));
  box.appendChild(_el('p', 'wm-gate-sub',
    '公開されると、専用URLからご覧いただけます。'));
  container.replaceChildren(box);
}

function _renderInvalidWeekId(container) {
  const box = _el('div', 'wm-gate-box');
  box.appendChild(_el('p', 'wm-gate-msg',
    'URLが正しくありません。'));
  const a = document.createElement('a');
  a.href = 'index.html';
  a.className = 'wm-link';
  a.textContent = 'トップページへ';
  box.appendChild(a);
  container.replaceChildren(box);
}

function _renderLoginRequired(container) {
  const box = _el('div', 'wm-gate-box');
  box.appendChild(_el('p', 'wm-gate-msg',
    'Weekly Marketcastをご覧いただくにはログインが必要です。'));
  const links = [
    { text: 'ログインする', href: 'login.html' },
    { text: '新規登録はこちら', href: 'signup.html' },
  ];
  for (const { text, href } of links) {
    const a = document.createElement('a');
    a.href = href;
    a.className = 'wm-gate-link';
    a.textContent = text;
    box.appendChild(a);
    box.appendChild(document.createTextNode(' '));
  }
  container.replaceChildren(box);
}

function _renderUpgradeRequired(container) {
  const box = _el('div', 'wm-gate-box');
  box.appendChild(_el('h2', 'wm-gate-title', '有料プランでご利用いただけます'));
  box.appendChild(_el('p', 'wm-gate-msg',
    'Weekly Marketcastは、マクロイベントと市場反応を毎週体系的に整理した有料会員向けレポートです。'));
  box.appendChild(_el('p', 'wm-gate-sub',
    '過去の類似局面との比較、6資産の動向整理、考察ポイントを毎週お届けします。'));
  const links = [
    { text: '有料プランを見る', href: 'pricing.html' },
    { text: 'アカウントを確認する', href: 'account.html' },
  ];
  for (const { text, href } of links) {
    const a = document.createElement('a');
    a.href = href;
    a.className = 'wm-gate-link';
    a.textContent = text;
    box.appendChild(a);
    box.appendChild(document.createTextNode(' '));
  }
  container.replaceChildren(box);
}

function _renderNotPublished(container) {
  const box = _el('div', 'wm-gate-box');
  box.appendChild(_el('p', 'wm-gate-msg',
    'このWeekly Marketcastは現在公開されていません。'));
  const a = document.createElement('a');
  a.href = 'index.html';
  a.className = 'wm-link';
  a.textContent = 'トップページへ';
  box.appendChild(a);
  container.replaceChildren(box);
}

function _renderGenericError(container) {
  const box = _el('div', 'wm-error-box');
  box.setAttribute('role', 'alert');
  box.appendChild(_el('p', 'wm-error-msg',
    'ページを正しく読み込めませんでした。'));
  const a = document.createElement('a');
  a.href = 'index.html';
  a.className = 'wm-link';
  a.textContent = 'トップページへ';
  box.appendChild(a);
  container.replaceChildren(box);
}

function _renderNetworkError(container) {
  const box = _el('div', 'wm-error-box');
  box.setAttribute('role', 'alert');
  box.appendChild(_el('p', 'wm-error-msg',
    'Weekly Marketcastを読み込めませんでした。時間をおいて再度お試しください。'));
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'wm-retry-btn';
  btn.textContent = '再試行する';
  box.appendChild(btn);
  container.replaceChildren(box);
}

function _renderUnexpectedError(container) {
  const box = _el('div', 'wm-error-box');
  box.setAttribute('role', 'alert');
  box.appendChild(_el('p', 'wm-error-msg',
    'Weekly Marketcastを読み込めませんでした。時間をおいて再度お試しください。'));
  container.replaceChildren(box);
}

/* ─── レポート描画 ──────────────────────────────────────────────────────── */

function _renderReport(container, data) {
  const frag = document.createDocumentFragment();

  // ヘッダー情報
  frag.appendChild(_buildReportHeader(data));

  const pb = data.paid_body;

  // summary
  if (typeof pb.summary === 'string' && pb.summary.trim()) {
    frag.appendChild(_buildSection('今週のまとめ', _buildSummary(pb.summary)));
  }

  // themes
  if (Array.isArray(pb.themes) && pb.themes.length > 0) {
    frag.appendChild(_buildSection('注目テーマ', _buildThemes(pb.themes)));
  }

  // asset_summaries
  if (Array.isArray(pb.asset_summaries) && pb.asset_summaries.length > 0) {
    frag.appendChild(_buildSection('資産別動向', _buildAssets(pb.asset_summaries)));
  }

  // similar_events
  if (Array.isArray(pb.similar_events) && pb.similar_events.length > 0) {
    frag.appendChild(_buildSection('類似過去局面', _buildSimilarEvents(pb.similar_events)));
  }

  // observation_points
  if (Array.isArray(pb.observation_points) && pb.observation_points.length > 0) {
    frag.appendChild(_buildSection('観察ポイント', _buildObservationPoints(pb.observation_points)));
  }

  // disclaimer（必ず表示）
  if (typeof pb.disclaimer === 'string') {
    frag.appendChild(_buildDisclaimer(pb.disclaimer));
  }

  container.replaceChildren(frag);
}

/* ─── レポートヘッダー ──────────────────────────────────────────────────── */

function _buildReportHeader(data) {
  const head = _el('div', 'wm-report-head');

  const eyebrow = _el('div', 'wm-eyebrow', 'Weekly Marketcast');
  head.appendChild(eyebrow);

  if (typeof data.title === 'string' && data.title.trim()) {
    head.appendChild(_el('h1', 'wm-title', data.title));
  } else {
    head.appendChild(_el('h1', 'wm-title', 'Weekly Marketcast'));
  }

  const meta = _el('div', 'wm-meta');
  if (typeof data.period_start === 'string' && typeof data.period_end === 'string') {
    meta.appendChild(_el('span', 'wm-meta-item',
      `対象期間: ${data.period_start} 〜 ${data.period_end}`));
  }
  if (typeof data.published_at === 'string') {
    const d = data.published_at.slice(0, 10);
    meta.appendChild(_el('span', 'wm-meta-sep', '｜'));
    meta.appendChild(_el('span', 'wm-meta-item', `公開日: ${d}`));
  }
  if (typeof data.revision === 'number') {
    meta.appendChild(_el('span', 'wm-meta-sep', '｜'));
    meta.appendChild(_el('span', 'wm-meta-item', `Rev. ${data.revision}`));
  }
  head.appendChild(meta);

  head.appendChild(_el('p', 'wm-report-desc',
    '本レポートは、当週のマクロイベントと市場反応を記録・整理したものです。将来を予測するものではありません。'));

  return head;
}

/* ─── セクション共通ラッパー ────────────────────────────────────────────── */

function _buildSection(labelText, content) {
  const wrap = _el('div', 'wm-section');
  wrap.appendChild(_el('div', 'wm-section-label', labelText));
  wrap.appendChild(content);
  return wrap;
}

/* ─── summary ───────────────────────────────────────────────────────────── */

function _buildSummary(text) {
  const p = _el('p', 'wm-summary-text', text);
  return p;
}

/* ─── themes ────────────────────────────────────────────────────────────── */

function _buildThemes(themes) {
  const list = _el('div', 'wm-themes-list');
  for (const theme of themes) {
    if (typeof theme !== 'object' || theme === null || Array.isArray(theme)) continue;
    const card = _el('div', 'wm-theme-card');
    if (typeof theme.title === 'string' && theme.title.trim()) {
      card.appendChild(_el('div', 'wm-theme-title', theme.title));
    }
    if (typeof theme.description === 'string' && theme.description.trim()) {
      card.appendChild(_el('p', 'wm-theme-desc', theme.description));
    }
    list.appendChild(card);
  }
  return list;
}

/* ─── asset_summaries ───────────────────────────────────────────────────── */

function _buildAssets(assetSummaries) {
  const grid = _el('div', 'wm-asset-grid');

  for (const s of assetSummaries) {
    if (typeof s !== 'object' || s === null || Array.isArray(s)) continue;
    const key = s.asset_key;
    if (!VALID_ASSET_KEYS.has(key)) continue;

    const card = _el('div', 'wm-asset-card');

    // アセット名（固定表示名を使用）
    card.appendChild(_el('div', 'wm-asset-label', ASSET_LABELS[key]));

    // direction のみ表示（end_value は表示しない）
    const dir = s.direction;
    if (typeof dir === 'string' && DIR_TEXT[dir] !== undefined) {
      const span = _el('span', 'wm-asset-dir ' + (DIR_CLASS[dir] || 'dir-na'), DIR_TEXT[dir]);
      card.appendChild(span);
    }

    // name（既存レポートに含まれる場合、副次表示として使用）
    if (typeof s.name === 'string' && s.name.trim() && s.name !== ASSET_LABELS[key]) {
      card.appendChild(_el('div', 'wm-asset-name-sub', s.name));
    }

    grid.appendChild(card);
  }

  return grid;
}

/* ─── similar_events ────────────────────────────────────────────────────── */

function _buildSimilarEvents(events) {
  const list = _el('div', 'wm-events-list');

  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    if (typeof ev !== 'object' || ev === null || Array.isArray(ev)) continue;

    const card = _el('div', 'wm-event-card');

    // ヘッダー
    const head = _el('div', 'wm-event-head');
    head.appendChild(_el('span', 'wm-event-num', `類似局面 ${i + 1}`));
    if (typeof ev.period === 'string' && ev.period.trim()) {
      head.appendChild(_el('span', 'wm-event-period', ev.period));
    }
    card.appendChild(head);

    if (typeof ev.description === 'string' && ev.description.trim()) {
      card.appendChild(_el('p', 'wm-event-desc', ev.description));
    }

    // timelines
    if (ev.timelines && typeof ev.timelines === 'object' && !Array.isArray(ev.timelines)) {
      const tlEl = _buildTimelines(ev.timelines);
      if (tlEl) card.appendChild(tlEl);
    }

    list.appendChild(card);
  }

  return list;
}

function _buildTimelines(timelines) {
  // 正式 6 資産のみ表示
  const rows = [];
  for (const key of ['wti', 'gold', 'sp500', 'ust10y', 'usdjpy', 'vix']) {
    const tl = timelines[key];
    if (!tl || typeof tl !== 'object' || Array.isArray(tl)) continue;

    // direction値が存在するか確認
    const hasDirs = PERIOD_KEYS.some(p => typeof tl[p] === 'string' && DIR_TEXT[tl[p]] !== undefined);
    if (!hasDirs) continue;

    rows.push({ key, tl });
  }

  if (rows.length === 0) return null;

  const wrap = _el('div', 'wm-tl-wrap');
  wrap.appendChild(_el('div', 'wm-tl-label', '資産別推移（過去局面発生後）'));

  const table = document.createElement('table');
  table.className = 'wm-tl-table';

  // ヘッダー行
  const thead = document.createElement('thead');
  const hrow = document.createElement('tr');
  const thAsset = document.createElement('th');
  thAsset.scope = 'col';
  thAsset.textContent = '資産';
  hrow.appendChild(thAsset);
  for (const p of PERIOD_KEYS) {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = PERIOD_LABELS[p];
    hrow.appendChild(th);
  }
  thead.appendChild(hrow);
  table.appendChild(thead);

  // データ行
  const tbody = document.createElement('tbody');
  for (const { key, tl } of rows) {
    const tr = document.createElement('tr');

    const tdName = document.createElement('td');
    tdName.textContent = ASSET_LABELS[key] || key;
    tr.appendChild(tdName);

    for (const p of PERIOD_KEYS) {
      const td = document.createElement('td');
      const dir = tl[p];
      if (typeof dir === 'string' && DIR_TEXT[dir] !== undefined) {
        td.className = DIR_CLASS[dir] || '';
        td.setAttribute('aria-label', `${PERIOD_LABELS[p]}: ${dir}`);
        td.textContent = DIR_TEXT[dir];
      } else {
        td.textContent = '—';
      }
      tr.appendChild(td);
    }

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);

  // mid_term_reversal 注記
  const reversals = rows.filter(({ tl }) => tl.mid_term_reversal === true)
    .map(({ key }) => ASSET_LABELS[key] || key);
  if (reversals.length > 0) {
    const note = _el('div', 'wm-tl-reversal',
      `中期反転: ${reversals.join('、')}（初動と中期で方向が異なります）`);
    wrap.appendChild(note);
  }

  wrap.appendChild(_el('div', 'wm-tl-note',
    '方向表示（↑↓→）は過去の価格変化の方向を記録したものです。売買推奨ではありません。'));

  return wrap;
}

/* ─── observation_points ────────────────────────────────────────────────── */

function _buildObservationPoints(points) {
  const list = _el('div', 'wm-obs-list');

  for (let i = 0; i < points.length; i++) {
    const pt = points[i];
    if (typeof pt !== 'object' || pt === null || Array.isArray(pt)) continue;

    const item = _el('div', 'wm-obs-item');
    item.appendChild(_el('span', 'wm-obs-num', `${i + 1}.`));
    const body = _el('div', 'wm-obs-body');
    if (typeof pt.title === 'string' && pt.title.trim()) {
      body.appendChild(_el('div', 'wm-obs-title', pt.title));
    }
    if (typeof pt.description === 'string' && pt.description.trim()) {
      body.appendChild(_el('p', 'wm-obs-desc', pt.description));
    }
    item.appendChild(body);
    list.appendChild(item);
  }

  return list;
}

/* ─── disclaimer ────────────────────────────────────────────────────────── */

function _buildDisclaimer(text) {
  const wrap = _el('div', 'wm-disclaimer');
  wrap.appendChild(_el('div', 'wm-disclaimer-label', '免責事項'));
  wrap.appendChild(_el('p', 'wm-disclaimer-text', text));
  return wrap;
}

/* ─── DOM ヘルパー ──────────────────────────────────────────────────────── */

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
