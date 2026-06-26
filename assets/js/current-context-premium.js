/**
 * current-context-premium.js
 *
 * 有料ユーザー向け詳細類似局面 UI モジュール。
 * current_context.html の IIFE から import して使用する。
 *
 * 責務:
 *   - subscription state に応じた premium セクションの描画
 *   - paid 状態で get-similar-matches Edge Function を呼び出す
 *   - top5 マッチカードの XSS 安全な DOM 構築
 *   - ページメモリキャッシュ（localStorage 不使用）
 *   - 重複リクエスト防止
 *
 * セキュリティ:
 *   - innerHTML による動的文字列の挿入を行わない（textContent / createElement）
 *   - cause_tag は allowedSet（current_context_public.json の free_top_match キー）で検証後に送信
 *   - API エラー詳細（内部メッセージ等）を UI に露出しない
 *   - DOM 非表示を認可手段として使用しない（認可は API が担う）
 */

import { supabase } from './supabase-client.js';
import { validateMatchResponse, sanitizeCauseTag } from './premium-validation.js';

/* ─── 定数 ─────────────────────────────────────────────────────────────── */

const AXIS_LABELS   = { vix: 'VIX', oil: '原油', rate: '金利' };
const ASSET_ORDER   = ['wti', 'gold', 'sp500', 'ust10y', 'usdjpy', 'vix'];
const PERIOD_KEYS   = ['d1', 'd7', 'd30', 'd90'];
const PERIOD_LABELS = { d1: '1日', d7: '7日', d30: '30日', d90: '90日' };

/* ─── ページメモリキャッシュ ─────────────────────────────────────────────── */

const _cache   = new Map(); // Map<causeTag, { matches, scoring }>
const _pending = new Set(); // Set<causeTag> — 進行中リクエスト

/* ─── エントリポイント ──────────────────────────────────────────────────── */

/**
 * premium セクションを初期化する。
 * current_context.html の IIFE から、subscription 確定後に呼ぶ。
 *
 * @param {string}      state      - subscription state（6種）
 * @param {string}      cause      - URL param cause（VALID_CAUSES 検証済み）
 * @param {Set<string>} allowedSet - free_top_match のキー集合
 * @param {HTMLElement} container  - #premium-section 要素
 */
export function setupPremiumSection(state, cause, allowedSet, container) {
  if (!container) return;
  container.replaceChildren(_buildContent(state, cause, allowedSet));
}

/* ─── 状態分岐 ──────────────────────────────────────────────────────────── */

function _buildContent(state, cause, allowedSet) {
  const frag = document.createDocumentFragment();

  frag.appendChild(_el('div', 'section-label', '詳細比較（類似局面 上位5件）'));

  const panel = _el('div', 'premium-panel');

  if (state === 'paid') {
    panel.appendChild(_buildPaidPanel(cause, allowedSet));
  } else if (state === 'unauthenticated') {
    panel.appendChild(_buildGateBox(
      'ログインして詳細比較（現在の市場環境に近い過去局面 上位5件）をご確認いただけます。',
      [{ text: 'ログインする', href: 'login.html' },
       { text: 'プランを見る', href: 'pricing.html' }]
    ));
  } else if (state === 'free') {
    panel.appendChild(_buildGateBox(
      '有料プランでは、現在の市場環境に最も近い過去局面 上位5件の詳細（資産反応データ・考察）をご確認いただけます。',
      [{ text: '有料プランを見る', href: 'pricing.html' }]
    ));
  } else if (state === 'attention') {
    panel.appendChild(_buildGateBox(
      'お支払い状況のご確認をお願いします。確認後に詳細比較をご利用いただけます。',
      [{ text: 'アカウントを確認する', href: 'account.html' }],
      true
    ));
  } else if (state === 'inactive') {
    panel.appendChild(_buildGateBox(
      '現在有効な有料プランがありません。',
      [{ text: '有料プランを見る', href: 'pricing.html' }]
    ));
  } else {
    /* error */
    panel.appendChild(_buildGateBox(
      '契約状況の確認中にエラーが発生しました。ページを再読み込みしてお試しください。',
      []
    ));
  }

  frag.appendChild(panel);
  return frag;
}

/* ─── paid パネル ───────────────────────────────────────────────────────── */

function _buildPaidPanel(cause, allowedSet) {
  const wrap = _el('div', 'premium-paid-wrap');

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'premium-load-btn';
  btn.textContent = '詳細比較を見る（上位5件）';
  /* data-cause-tag は sanitizeCauseTag で検証済みの値のみ設定 */
  const safeCause = sanitizeCauseTag(cause, allowedSet);
  btn.setAttribute('data-cause-tag', safeCause ?? '');
  wrap.appendChild(btn);

  const resultArea = _el('div', 'premium-result-area');
  resultArea.setAttribute('aria-live', 'polite');
  wrap.appendChild(resultArea);

  if (!safeCause) {
    /* cause_tag が allowedSet にない場合はボタンを無効化 */
    btn.disabled = true;
    btn.setAttribute('aria-disabled', 'true');
    return wrap;
  }

  btn.addEventListener('click', () => {
    _handleLoad(btn, safeCause, resultArea);
  });

  return wrap;
}

/* ─── ロードハンドラ ─────────────────────────────────────────────────────── */

async function _handleLoad(btn, cause, resultArea) {
  /* キャッシュヒット */
  if (_cache.has(cause)) {
    _renderMatches(resultArea, _cache.get(cause));
    return;
  }

  /* 重複リクエスト防止 */
  if (_pending.has(cause)) return;

  /* ローディング状態 */
  _pending.add(cause);
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
  _renderLoading(resultArea);

  try {
    const { data, error } = await supabase.functions.invoke('get-similar-matches', {
      body: { cause_tag: cause },
    });

    if (error) {
      const status = error.context?.status ?? error.status ?? null;
      if (status === 403) {
        _cache.clear();
      }
      _renderError(resultArea, status);
      return;
    }

    if (!validateMatchResponse(data)) {
      _renderError(resultArea, 'invalid');
      return;
    }

    const payload = {
      matches: data.matches.slice(0, 5),
      scoring: data.scoring ?? {},
    };
    _cache.set(cause, payload);
    _renderMatches(resultArea, payload);

  } catch (_) {
    _renderError(resultArea, 'network');
  } finally {
    _pending.delete(cause);
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
  }
}

/* ─── 描画関数 ──────────────────────────────────────────────────────────── */

function _renderLoading(area) {
  const d = _el('div', 'pm-loading', '読み込み中…');
  d.setAttribute('aria-busy', 'true');
  area.replaceChildren(d);
}

function _renderError(area, status) {
  const box = _el('div', 'pm-error');
  box.setAttribute('role', 'alert');

  let msg;
  if (status === 401) {
    msg = 'セッションが切れました。ページを再読み込みしてログインし直してください。';
  } else if (status === 403) {
    msg = '現在のプランではこの機能を利用できません。';
    const link = document.createElement('a');
    link.href = 'account.html';
    link.className = 'pm-error-link';
    link.textContent = 'アカウントを確認する';
    box.appendChild(_el('span', '', msg));
    box.appendChild(link);
    area.replaceChildren(box);
    return;
  } else if (status === 400 || status === 'invalid') {
    msg = 'この比較を読み込めませんでした。';
  } else if (status === 503) {
    msg = 'サービスが一時的に利用できません。しばらく後に再試行してください。';
  } else {
    msg = '通信エラーが発生しました。ページを再読み込みしてお試しください。';
  }

  box.appendChild(_el('span', '', msg));
  area.replaceChildren(box);
}

function _renderMatches(area, payload) {
  const frag = document.createDocumentFragment();

  if (!payload.matches || payload.matches.length === 0) {
    frag.appendChild(_el('div', 'pm-empty', '現在この条件に一致する類似局面は見つかりませんでした。'));
    area.replaceChildren(frag);
    return;
  }

  const maxScore = payload.scoring?.max_score ?? 3;

  for (const m of payload.matches) {
    frag.appendChild(_buildMatchCard(m, maxScore));
  }

  area.replaceChildren(frag);
}

/* ─── マッチカード ──────────────────────────────────────────────────────── */

function _buildMatchCard(m, maxScore) {
  const card = _el('div', 'pm-card');

  /* ヘッド */
  const head = _el('div', 'pm-card-head');

  head.appendChild(_el('div', 'pm-rank', '#' + m.rank));
  head.appendChild(_el('p', 'pm-name', m.name));

  if (m.date) {
    head.appendChild(_el('div', 'pm-date', '発生日：' + m.date));
  }

  head.appendChild(_el('div', 'pm-score',
    'スコア：' + m.score + ' / ' + maxScore));

  /* 軸チップ */
  const axesWrap = _el('div', 'pm-axes');
  if (Array.isArray(m.matched_axes)) {
    for (const ax of m.matched_axes) {
      const chip = _el('span', 'pm-axis-chip pm-axis-match', AXIS_LABELS[ax] ?? ax);
      chip.setAttribute('title', '一致した軸');
      axesWrap.appendChild(chip);
    }
  }
  if (Array.isArray(m.unmatched_axes)) {
    for (const ax of m.unmatched_axes) {
      const chip = _el('span', 'pm-axis-chip pm-axis-miss', AXIS_LABELS[ax] ?? ax);
      chip.setAttribute('title', '不一致の軸');
      axesWrap.appendChild(chip);
    }
  }
  head.appendChild(axesWrap);

  card.appendChild(head);

  /* ボディ */
  const body = _el('div', 'pm-card-body');

  /* イベント詳細リンク */
  const link = document.createElement('a');
  link.className = 'pm-detail-link';
  link.textContent = 'イベント詳細を見る →';
  link.href = 'event_detail.html?id=' + encodeURIComponent(m.event_id);
  body.appendChild(link);

  /* テキスト項目（null/空の場合は非表示） */
  const textFields = [
    { key: 'similarity_reason', label: '類似している理由' },
    { key: 'why_reaction',      label: '市場反応の解説' },
    { key: 'key_insight',       label: 'キーインサイト' },
  ];
  for (const { key, label } of textFields) {
    const val = m[key];
    if (typeof val === 'string' && val.trim().length > 0) {
      body.appendChild(_el('div', 'pm-text-label', label));
      body.appendChild(_el('p', 'pm-text', val));
    }
  }

  /* 資産反応テーブル */
  if (m.reactions && typeof m.reactions === 'object' && !Array.isArray(m.reactions)) {
    const rxnEl = _buildReactionsTable(m.reactions);
    if (rxnEl) body.appendChild(rxnEl);
  }

  card.appendChild(body);
  return card;
}

/* ─── 資産反応テーブル ───────────────────────────────────────────────────── */

function _buildReactionsTable(reactions) {
  const rows = [];
  for (const key of ASSET_ORDER) {
    const r = reactions[key];
    if (!r || typeof r !== 'object' || Array.isArray(r)) continue;
    if (r.status !== 'ok') continue;
    const changes = r.changes;
    if (!changes || typeof changes !== 'object') continue;
    const hasAny = PERIOD_KEYS.some(p => typeof changes[p] === 'number');
    if (!hasAny) continue;
    rows.push({ key, label: r.label ?? key, changes, changes_pt: r.changes_pt ?? null });
  }

  if (rows.length === 0) return null;

  const section = _el('div', 'pm-reactions');
  section.appendChild(_el('div', 'pm-reactions-label', '資産別変化率（過去局面発生後）'));

  const table = document.createElement('table');
  table.className = 'pm-rxn-table';

  /* ヘッダ行 */
  const thead = document.createElement('thead');
  const hrow  = document.createElement('tr');
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

  /* データ行 */
  const tbody = document.createElement('tbody');
  for (const row of rows) {
    const tr = document.createElement('tr');

    const tdName = document.createElement('td');
    tdName.textContent = row.label;
    tr.appendChild(tdName);

    for (const p of PERIOD_KEYS) {
      const td = document.createElement('td');
      const val = row.changes[p];
      if (typeof val !== 'number') {
        td.textContent = '—';
      } else {
        const sign = val > 0 ? '+' : '';
        let text = sign + val.toFixed(1) + '%';
        /* 債券は pt も括弧付きで表示 */
        if (row.changes_pt && typeof row.changes_pt[p] === 'number') {
          const pt = row.changes_pt[p];
          const ptSign = pt > 0 ? '+' : '';
          text += ' (' + ptSign + pt.toFixed(2) + 'pt)';
        }
        td.textContent = text;
        /* 色は色だけに依存しない（記号 + クラス） */
        if (val > 0) td.className = 'up';
        else if (val < 0) td.className = 'down';
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  section.appendChild(table);

  return section;
}

/* ─── ゲートボックス（非paid状態） ─────────────────────────────────────── */

function _buildGateBox(message, links, isAttention = false) {
  const box = _el('div', isAttention ? 'premium-gate-box premium-gate-attention' : 'premium-gate-box');
  box.appendChild(_el('p', 'premium-gate-msg', message));
  for (const { text, href } of links) {
    const a = document.createElement('a');
    a.className = 'premium-gate-link';
    a.textContent = text;
    a.href = href;
    box.appendChild(a);
    /* リンク間のスペース */
    box.appendChild(document.createTextNode(' '));
  }
  return box;
}

/* ─── DOM ヘルパー ──────────────────────────────────────────────────────── */

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
