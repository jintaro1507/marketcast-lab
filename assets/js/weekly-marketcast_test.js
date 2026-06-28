/**
 * weekly-marketcast_test.js — weekly-marketcast.js のユニットテスト
 *
 * 実行コマンド（プロジェクトルートから）:
 *   deno test assets/js/weekly-marketcast_test.js
 */

import { assertEquals, assertStrictEquals } from 'jsr:@std/assert@1';
import {
  isValidWeekId,
  hasForbiddenKey,
  validateWeeklyResponse,
} from './weekly-marketcast.js';

// ─── フィクスチャ ──────────────────────────────────────────────────────────

const VALID_PAID_BODY = {
  summary: '今週のまとめテキスト',
  asset_summaries: [
    { asset_key: 'wti',    name: 'WTI Crude Oil',        direction: 'flat', end_value: null },
    { asset_key: 'gold',   name: 'Gold',                  direction: 'flat', end_value: null },
    { asset_key: 'sp500',  name: 'S&P 500',               direction: 'up',   end_value: null },
    { asset_key: 'ust10y', name: 'US 10Y Yield',          direction: 'flat', end_value: null },
    { asset_key: 'usdjpy', name: 'USD/JPY',               direction: 'up',   end_value: null },
    { asset_key: 'vix',    name: 'VIX',                   direction: 'na',   end_value: null },
  ],
  themes: [{ title: 'テーマA', description: 'テーマの説明' }],
  similar_events: [{
    period: '2020年3月',
    description: '過去局面の説明',
    timelines: {
      gold:  { d1: 'flat', d7: 'up',  d30: 'flat', d90: 'up',  mid_term_reversal: false },
      sp500: { d1: 'up',   d7: 'up',  d30: 'up',   d90: 'up',  mid_term_reversal: false },
    },
  }],
  observation_points: [
    { title: '観察点1', description: '説明1' },
    { title: '観察点2', description: '説明2' },
    { title: '観察点3', description: '説明3' },
  ],
  disclaimer: 'テスト免責事項',
};

function makeResponse(overrides = {}) {
  return Object.assign({
    week_id:        '2026-W01',
    revision:       1,
    title:          'Weekly Marketcast 2026-W01',
    period_start:   '2025-12-29',
    period_end:     '2026-01-04',
    published_at:   '2026-01-05T00:00:00Z',
    paid_body:      structuredClone(VALID_PAID_BODY),
    teaser_hash:    'a'.repeat(64),
    paid_body_hash: 'b'.repeat(64),
  }, overrides);
}

// ─── isValidWeekId ────────────────────────────────────────────────────────

Deno.test('isValidWeekId: 正常 week_id', () => {
  assertEquals(isValidWeekId('2026-W01'), true);
  assertEquals(isValidWeekId('2026-W26'), true);
  assertEquals(isValidWeekId('2026-W52'), true);
  assertEquals(isValidWeekId('2020-W53'), true);
});

Deno.test('isValidWeekId: W00 は無効', () => {
  assertEquals(isValidWeekId('2026-W00'), false);
});

Deno.test('isValidWeekId: W54 は無効', () => {
  assertEquals(isValidWeekId('2026-W54'), false);
});

Deno.test('isValidWeekId: 形式不正', () => {
  assertEquals(isValidWeekId('2026-26'), false);
  assertEquals(isValidWeekId('26-W01'), false);
  assertEquals(isValidWeekId('2026W01'), false);
  assertEquals(isValidWeekId(''), false);
  assertEquals(isValidWeekId('2026-w01'), false);
});

Deno.test('isValidWeekId: 数値は false', () => {
  // @ts-ignore
  assertEquals(isValidWeekId(202601), false);
  // @ts-ignore
  assertEquals(isValidWeekId(null), false);
});

Deno.test('isValidWeekId: XSS 試みは false', () => {
  assertEquals(isValidWeekId('<script>alert(1)</script>'), false);
  assertEquals(isValidWeekId('2026-W01; DROP TABLE--'), false);
});

// ─── hasForbiddenKey ──────────────────────────────────────────────────────

Deno.test('hasForbiddenKey: forbidden key なし → false', () => {
  assertEquals(hasForbiddenKey({ summary: 'ok', direction: 'up' }), false);
});

Deno.test('hasForbiddenKey: value キーを含む → true', () => {
  assertEquals(hasForbiddenKey({ value: 123 }), true);
});

Deno.test('hasForbiddenKey: current_value を含む → true', () => {
  assertEquals(hasForbiddenKey({ asset: { current_value: 50 } }), true);
});

Deno.test('hasForbiddenKey: price を含む → true', () => {
  assertEquals(hasForbiddenKey({ price: 100 }), true);
});

Deno.test('hasForbiddenKey: jwt を含む → true', () => {
  assertEquals(hasForbiddenKey({ jwt: 'token' }), true);
});

Deno.test('hasForbiddenKey: authorization を含む → true', () => {
  assertEquals(hasForbiddenKey({ authorization: 'Bearer xxx' }), true);
});

Deno.test('hasForbiddenKey: 配列内のネスト → true', () => {
  assertEquals(hasForbiddenKey([{ nested: { raw_value: 1 } }]), true);
});

Deno.test('hasForbiddenKey: null は false', () => {
  assertEquals(hasForbiddenKey(null), false);
});

Deno.test('hasForbiddenKey: 文字列は false', () => {
  assertEquals(hasForbiddenKey('value'), false);
});

// ─── validateWeeklyResponse ───────────────────────────────────────────────

Deno.test('validateWeeklyResponse: 正常レスポンス → true', () => {
  assertEquals(validateWeeklyResponse(makeResponse()), true);
});

Deno.test('validateWeeklyResponse: null → false', () => {
  assertEquals(validateWeeklyResponse(null), false);
});

Deno.test('validateWeeklyResponse: 配列 → false', () => {
  assertEquals(validateWeeklyResponse([]), false);
});

Deno.test('validateWeeklyResponse: week_id 欠落 → false', () => {
  const r = makeResponse();
  delete r.week_id;
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: paid_body 欠落 → false', () => {
  const r = makeResponse();
  delete r.paid_body;
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: paid_body が文字列 → false', () => {
  assertEquals(validateWeeklyResponse(makeResponse({ paid_body: '{}' })), false);
});

Deno.test('validateWeeklyResponse: paid_body.summary 欠落 → false', () => {
  const r = makeResponse();
  delete r.paid_body.summary;
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: paid_body.asset_summaries 欠落 → false', () => {
  const r = makeResponse();
  delete r.paid_body.asset_summaries;
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: paid_body.themes 欠落 → false', () => {
  const r = makeResponse();
  delete r.paid_body.themes;
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: paid_body.similar_events 欠落 → false', () => {
  const r = makeResponse();
  delete r.paid_body.similar_events;
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: paid_body.observation_points 欠落 → false', () => {
  const r = makeResponse();
  delete r.paid_body.observation_points;
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: paid_body.disclaimer 欠落 → false', () => {
  const r = makeResponse();
  delete r.paid_body.disclaimer;
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: forbidden key を含む → false', () => {
  const r = makeResponse();
  r.paid_body.asset_summaries[0].value = 50.5;
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: price キーを含む → false', () => {
  const r = makeResponse();
  r.paid_body.price = 100;
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: asset_summaries が配列でない → false', () => {
  const r = makeResponse();
  r.paid_body.asset_summaries = {};
  assertEquals(validateWeeklyResponse(r), false);
});

Deno.test('validateWeeklyResponse: teaser_hash は内部列だが存在しても通る（API が返す）', () => {
  // teaser_hash / paid_body_hash はAPIが返すが表示しない
  // validateWeeklyResponse は表示検証ではなく構造検証
  assertEquals(validateWeeklyResponse(makeResponse()), true);
});

// ─── 静的セキュリティ確認（ソースコード検査） ────────────────────────────

const SRC_PATH = new URL('./weekly-marketcast.js', import.meta.url).pathname;
const src = await Deno.readTextFile(SRC_PATH);

Deno.test('静的: innerHTML を使用していない', () => {
  const hasInnerHTML = src.includes('.innerHTML');
  assertEquals(hasInnerHTML, false, 'innerHTML が使用されています');
});

Deno.test('静的: console.log に session・token・jwt を出力しない', () => {
  const lines = src.split('\n');
  const dangerous = lines.filter(l => {
    const lo = l.toLowerCase();
    return lo.includes('console.log') && (
      lo.includes('session') ||
      lo.includes('token') ||
      lo.includes('jwt') ||
      lo.includes('access_token')
    );
  });
  assertEquals(dangerous.length, 0, `危険な console.log: ${dangerous.join('\n')}`);
});

Deno.test('静的: console.log に paid_body を出力しない', () => {
  const lines = src.split('\n');
  const dangerous = lines.filter(l =>
    l.includes('console.log') && l.includes('paid_body')
  );
  assertEquals(dangerous.length, 0, `paid_body を console.log しています: ${dangerous.join('\n')}`);
});

Deno.test('静的: service_role_key の実値をハードコードしていない', () => {
  // 禁止キー名として 'service_role_key' が FORBIDDEN_DISPLAY_KEYS に含まれるのは正当。
  // シークレット形式（eyJ... や 長大な Base64 文字列）が含まれていないことを確認。
  const hasSecret = /eyJ[A-Za-z0-9+/]{40,}/.test(src);
  assertEquals(hasSecret, false, 'JWT シークレット形式の文字列が含まれています');
});

Deno.test('静的: end_value を表示するコードがない', () => {
  // end_value という文字列を表示系の関数（textContent）に渡していないか検査
  // (定数定義や null チェックは許可)
  const hasDisplay = src.split('\n').some(l =>
    l.includes('textContent') && l.includes('end_value')
  );
  assertEquals(hasDisplay, false, 'end_value を textContent で表示しています');
});

Deno.test('静的: teaser_hash・paid_body_hash を表示するコードがない', () => {
  const lines = src.split('\n');
  const displaying = lines.filter(l =>
    l.includes('textContent') && (l.includes('teaser_hash') || l.includes('paid_body_hash'))
  );
  assertEquals(displaying.length, 0, '内部列を表示しています');
});

Deno.test('静的: 正式 6 資産の asset_key が定義されている', () => {
  for (const key of ['wti', 'gold', 'sp500', 'ust10y', 'usdjpy', 'vix']) {
    assertEquals(src.includes(`'${key}'`), true, `${key} が見つかりません`);
  }
});

Deno.test('静的: Pages JSON 生成コードがない', () => {
  assertEquals(src.includes('current_context_public'), false);
  assertEquals(src.includes('writeFile'), false);
  assertEquals(src.includes('Deno.writeText'), false);
});

Deno.test('静的: 本番 week_id がハードコードされていない', () => {
  // 実在週 ID（2020年以降）のハードコードを検査
  const realWeekPattern = /202[0-9]-W\d{2}/g;
  const matches = src.match(realWeekPattern) || [];
  assertEquals(matches.length, 0, `本番 week_id がハードコードされています: ${matches.join(', ')}`);
});
