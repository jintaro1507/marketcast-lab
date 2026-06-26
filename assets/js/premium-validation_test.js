/**
 * premium-validation_test.js — validateMatchResponse / sanitizeCauseTag のユニットテスト
 *
 * 実行コマンド（プロジェクトルートから）:
 *   deno test assets/js/premium-validation_test.js
 */

import { assertEquals } from 'jsr:@std/assert@1';
import { validateMatchResponse, sanitizeCauseTag } from './premium-validation.js';

// ─── 有効レスポンスのフィクスチャ ────────────────────────────────────────

const VALID_MATCH = {
  rank: 1,
  event_id: 'svb_collapse_2023',
  name: 'SVB破綻',
  date: '2023-03-10',
  score: 3,
  matched_axes: ['vix', 'oil', 'rate'],
  unmatched_axes: [],
  reactions: { wti: { label: 'WTI原油', status: 'ok', changes: { d1: -2.8 } } },
  similarity_reason: '類似している',
  why_reaction: '反応の説明',
  key_insight: null,
};

function makeResponse(overrides = {}) {
  return Object.assign(
    { matches: [Object.assign({}, VALID_MATCH)], scoring: { max_score: 3 } },
    overrides,
  );
}

// ─── validateMatchResponse ────────────────────────────────────────────────

Deno.test('1: 有効レスポンス（matches 1件）は true', () => {
  assertEquals(validateMatchResponse(makeResponse()), true);
});

Deno.test('2: matches が空配列は true（0件は正常）', () => {
  assertEquals(validateMatchResponse(makeResponse({ matches: [] })), true);
});

Deno.test('3: matches が 5件は true', () => {
  const five = Array.from({ length: 5 }, (_, i) =>
    Object.assign({}, VALID_MATCH, { rank: i + 1, event_id: 'ev_' + i }),
  );
  assertEquals(validateMatchResponse(makeResponse({ matches: five })), true);
});

Deno.test('4: matches が 6件以上は false（仕様上 top5 超は不正）', () => {
  const six = Array.from({ length: 6 }, (_, i) =>
    Object.assign({}, VALID_MATCH, { rank: i + 1, event_id: 'ev_' + i }),
  );
  assertEquals(validateMatchResponse(makeResponse({ matches: six })), false);
});

Deno.test('5: data が null は false', () => {
  assertEquals(validateMatchResponse(null), false);
});

Deno.test('6: data が undefined は false', () => {
  assertEquals(validateMatchResponse(undefined), false);
});

Deno.test('7: data が配列は false（オブジェクトを期待）', () => {
  assertEquals(validateMatchResponse([]), false);
  assertEquals(validateMatchResponse([VALID_MATCH]), false);
});

Deno.test('8: data が文字列は false', () => {
  assertEquals(validateMatchResponse('{"matches":[]}'), false);
});

Deno.test('9: event_id が空文字列の match は false', () => {
  const m = Object.assign({}, VALID_MATCH, { event_id: '' });
  assertEquals(validateMatchResponse(makeResponse({ matches: [m] })), false);
});

Deno.test('10: score が文字列の match は false', () => {
  const m = Object.assign({}, VALID_MATCH, { score: '3' });
  assertEquals(validateMatchResponse(makeResponse({ matches: [m] })), false);
});

Deno.test('11: matched_axes がオブジェクトの match は false', () => {
  const m = Object.assign({}, VALID_MATCH, { matched_axes: { vix: true } });
  assertEquals(validateMatchResponse(makeResponse({ matches: [m] })), false);
});

Deno.test('12: reactions が null の match は false', () => {
  const m = Object.assign({}, VALID_MATCH, { reactions: null });
  assertEquals(validateMatchResponse(makeResponse({ matches: [m] })), false);
});

Deno.test('13: reactions が配列の match は false', () => {
  const m = Object.assign({}, VALID_MATCH, { reactions: [] });
  assertEquals(validateMatchResponse(makeResponse({ matches: [m] })), false);
});

Deno.test('14: key_insight が null でも true（テキスト項目は optional）', () => {
  const m = Object.assign({}, VALID_MATCH, { key_insight: null, similarity_reason: null });
  assertEquals(validateMatchResponse(makeResponse({ matches: [m] })), true);
});

// ─── sanitizeCauseTag ─────────────────────────────────────────────────────

const ALLOWED = new Set(['bank_crisis', 'supply_shock', 'war', 'pandemic']);

Deno.test('15a: 許可セットに存在する cause_tag を返す', () => {
  assertEquals(sanitizeCauseTag('bank_crisis', ALLOWED), 'bank_crisis');
  assertEquals(sanitizeCauseTag('pandemic', ALLOWED), 'pandemic');
});

Deno.test('15b: 許可セットに存在しない cause_tag は null', () => {
  assertEquals(sanitizeCauseTag('unknown_tag', ALLOWED), null);
  assertEquals(sanitizeCauseTag('BANK_CRISIS', ALLOWED), null); // 大文字は別扱い
});

Deno.test('15c: XSS 試みの文字列は null（allowedSet に存在しない）', () => {
  assertEquals(sanitizeCauseTag('<script>alert(1)</script>', ALLOWED), null);
  assertEquals(sanitizeCauseTag('"; DROP TABLE--', ALLOWED), null);
  assertEquals(sanitizeCauseTag('../etc/passwd', ALLOWED), null);
});

Deno.test('15d: 空文字列は null', () => {
  assertEquals(sanitizeCauseTag('', ALLOWED), null);
});

Deno.test('15e: cause が数値は null', () => {
  assertEquals(sanitizeCauseTag(123, ALLOWED), null);
  assertEquals(sanitizeCauseTag(null, ALLOWED), null);
});

Deno.test('15f: allowedSet が Set でない場合は null', () => {
  assertEquals(sanitizeCauseTag('bank_crisis', ['bank_crisis']), null);
  assertEquals(sanitizeCauseTag('bank_crisis', { bank_crisis: true }), null);
  assertEquals(sanitizeCauseTag('bank_crisis', null), null);
});
