/**
 * matching_test.ts — computeMatches と分類関数のユニットテスト
 *
 * 実行コマンド:
 *   deno test supabase/functions/get-similar-matches/matching_test.ts
 *
 * Python alignment: scripts/calculate_event_reactions.py の score_events_for_cause()
 * と同一入力・同一出力を検証する。
 * Python の出力（event_id 順）は事前に確認済み:
 *   bank_crisis / state={vix:elev, oil:mid, rate:high} →
 *     1. svb_collapse_2023  score=3
 *     2. asian_crisis_1997  score=2
 *     3. eurozone_crisis_2010 score=1  (tie: newer date 2010 > 1998)
 *     4. ltcm_crisis_1998  score=1
 *     5. lehman_shock_2008  score=0
 */

import { assertEquals } from 'jsr:@std/assert@1';
import {
  classifyVix,
  classifyOil,
  classifyRate,
  buildMarketStateTags,
  computeMatches,
  type ScoredEvent,
} from './matching.ts';

// ─── テスト用フィクスチャ ─────────────────────────────────────────────────────

/** data/event_reactions.json から実際の context_snapshot を持つイベントのみ抜粋。
 *  context_snapshot の値は実データと同じ（calculate_event_reactions.py による計算値）。
 */
const BANK_CRISIS_EVENTS: ScoredEvent[] = [
  {
    id: 'eurozone_crisis_2010',
    name: 'ユーロ圏債務危機',
    date: '2010-04-27',
    cause_tags: ['bank_crisis', 'debt_crisis'],
    context_snapshot: { vix_level: 22.0, oil_price_wti: 82.6, fed_funds_rate: 0.2, ust10y_yield: 3.83 },
    reactions: {},
    similarity_reason: null, why_reaction: null, key_insight: null,
  },
  {
    id: 'asian_crisis_1997',
    name: 'アジア通貨危機',
    date: '1997-07-02',
    cause_tags: ['bank_crisis', 'currency_crisis'],
    context_snapshot: { vix_level: 18.5, oil_price_wti: 19.2, fed_funds_rate: 5.5, ust10y_yield: 6.69 },
    reactions: {},
    similarity_reason: null, why_reaction: null, key_insight: null,
  },
  {
    id: 'ltcm_crisis_1998',
    name: 'LTCM危機',
    date: '1998-09-02',
    cause_tags: ['bank_crisis'],
    context_snapshot: { vix_level: 37.0, oil_price_wti: 14.0, fed_funds_rate: 5.5, ust10y_yield: 5.1 },
    reactions: {},
    similarity_reason: null, why_reaction: null, key_insight: null,
  },
  {
    id: 'lehman_shock_2008',
    name: 'リーマンショック',
    date: '2008-09-15',
    cause_tags: ['bank_crisis'],
    context_snapshot: { vix_level: 31.7, oil_price_wti: 95.7, fed_funds_rate: 2.0, ust10y_yield: 3.45 },
    reactions: {},
    similarity_reason: null, why_reaction: null, key_insight: null,
  },
  {
    id: 'svb_collapse_2023',
    name: 'SVB破綻',
    date: '2023-03-10',
    cause_tags: ['bank_crisis'],
    context_snapshot: { vix_level: 19.1, oil_price_wti: 76.7, fed_funds_rate: 4.58, ust10y_yield: 3.69 },
    reactions: {},
    similarity_reason: null, why_reaction: null, key_insight: null,
  },
];

const ALL_NULL_EVENT: ScoredEvent = {
  id: 'no_snapshot_event',
  name: 'スナップショットなし',
  date: '2022-01-01',
  cause_tags: ['bank_crisis'],
  context_snapshot: {},
  reactions: {}, similarity_reason: null, why_reaction: null, key_insight: null,
};

const OIL_SHOCK_1973: ScoredEvent = {
  id: 'oil_shock_1973',
  name: '第1次オイルショック',
  date: '1973-10-17',
  cause_tags: ['supply_shock', 'middle_east'],
  context_snapshot: {},
  reactions: {}, similarity_reason: null, why_reaction: null, key_insight: null,
};

// ─── Section 1: 分類関数 ─────────────────────────────────────────────────────

Deno.test('classifyVix: null/undefined → null', () => {
  assertEquals(classifyVix(null), null);
  assertEquals(classifyVix(undefined), null);
});

Deno.test('classifyVix: <15 → calm', () => assertEquals(classifyVix(14.9), 'calm'));
Deno.test('classifyVix: 15 → elev', () => assertEquals(classifyVix(15), 'elev'));
Deno.test('classifyVix: 24.9 → elev', () => assertEquals(classifyVix(24.9), 'elev'));
Deno.test('classifyVix: 25 → stress', () => assertEquals(classifyVix(25), 'stress'));
Deno.test('classifyVix: 39.9 → stress', () => assertEquals(classifyVix(39.9), 'stress'));
Deno.test('classifyVix: 40 → panic', () => assertEquals(classifyVix(40), 'panic'));

Deno.test('classifyOil: null/undefined → null', () => {
  assertEquals(classifyOil(null), null);
  assertEquals(classifyOil(undefined), null);
});
Deno.test('classifyOil: <40 → lo', () => assertEquals(classifyOil(39.9), 'lo'));
Deno.test('classifyOil: 40 → mid', () => assertEquals(classifyOil(40), 'mid'));
Deno.test('classifyOil: 79.9 → mid', () => assertEquals(classifyOil(79.9), 'mid'));
Deno.test('classifyOil: 80 → hi', () => assertEquals(classifyOil(80), 'hi'));

Deno.test('classifyRate: both null → null', () => assertEquals(classifyRate(null, null), null));
Deno.test('classifyRate: one null → use other', () => {
  assertEquals(classifyRate(null, 3.0), 'mid');
  assertEquals(classifyRate(3.0, null), 'mid');
});
Deno.test('classifyRate: avg<2 → low', () => assertEquals(classifyRate(1.0, 1.5), 'low'));
Deno.test('classifyRate: avg<4 → mid', () => assertEquals(classifyRate(2.0, 4.0), 'mid'));
Deno.test('classifyRate: avg>=4 → high', () => assertEquals(classifyRate(4.0, 5.0), 'high'));

Deno.test('buildMarketStateTags: all null → empty', () => {
  assertEquals(buildMarketStateTags(null, null, null, null), {});
});

Deno.test('buildMarketStateTags: full values', () => {
  // eurozone_crisis_2010: vix=22→elev, oil=82.6→hi, avg(0.2,3.83)=2.015→mid
  const tags = buildMarketStateTags(22.0, 82.6, 0.2, 3.83);
  assertEquals(tags, { vix: 'elev', oil: 'hi', rate: 'mid' });
});

// ─── Section 2: Python alignment（bank_crisis, state=elev/mid/high） ──────────

// Python 出力（確認済み）:
//   1. svb_collapse_2023  score=3 (vix=elev,oil=mid,rate=high で全一致)
//   2. asian_crisis_1997  score=2 (vix=elev,rate=high 一致; oil=lo≠mid)
//   3. eurozone_crisis_2010 score=1 (vix=elev のみ; oil=hi≠mid, rate=mid≠high)
//      tie-break: date 2010-04-27 > 1998-09-02 (ltcm)
//   4. ltcm_crisis_1998  score=1 (rate=high のみ; vix=stress≠elev, oil=lo≠mid)
//   5. lehman_shock_2008  score=0 (vix=stress,oil=hi,rate=mid — すべて不一致)

const STATE_ELEV_MID_HIGH = { vix: 'elev', oil: 'mid', rate: 'high' };

Deno.test('1: top5 を正しい順序で返す (Python alignment)', () => {
  const results = computeMatches('bank_crisis', STATE_ELEV_MID_HIGH, BANK_CRISIS_EVENTS);
  assertEquals(results.length, 5);
  assertEquals(results[0].event_id, 'svb_collapse_2023');
  assertEquals(results[0].score, 3);
  assertEquals(results[1].event_id, 'asian_crisis_1997');
  assertEquals(results[1].score, 2);
  assertEquals(results[2].event_id, 'eurozone_crisis_2010'); // tie-break: newer 2010 > 1998
  assertEquals(results[2].score, 1);
  assertEquals(results[3].event_id, 'ltcm_crisis_1998');
  assertEquals(results[3].score, 1);
  assertEquals(results[4].event_id, 'lehman_shock_2008');
  assertEquals(results[4].score, 0);
});

Deno.test('2: cause_tag が異なるイベントを除外', () => {
  const results = computeMatches('pandemic', STATE_ELEV_MID_HIGH, BANK_CRISIS_EVENTS);
  assertEquals(results.length, 0); // bank_crisis events に pandemic は含まれない
});

Deno.test('3: スコア降順', () => {
  const results = computeMatches('bank_crisis', STATE_ELEV_MID_HIGH, BANK_CRISIS_EVENTS);
  for (let i = 0; i < results.length - 1; i++) {
    // score は非増加（前の方が大きいか等しい）
    // score が同じ場合は n_comp とdate で決まるため score のみでは厳密降順ではない
    assertEquals(results[i].score >= results[i + 1].score, true, `rank${i+1} score not >= rank${i+2}`);
  }
});

Deno.test('4: 同点時に Python と同じ tie-break（date 降順）', () => {
  // eurozone(2010) と ltcm(1998) は score=1,n_comp=3 で同点 → eurozone が先
  const results = computeMatches('bank_crisis', STATE_ELEV_MID_HIGH, BANK_CRISIS_EVENTS);
  const eurozone = results.findIndex((r) => r.event_id === 'eurozone_crisis_2010');
  const ltcm = results.findIndex((r) => r.event_id === 'ltcm_crisis_1998');
  assertEquals(eurozone < ltcm, true, 'eurozone (2010) should rank before ltcm (1998)');
});

Deno.test('5: 5件を超えて返さない', () => {
  // 同じイベントを 10 件追加しても top5 のみ返す
  const manyEvents = [...BANK_CRISIS_EVENTS, ...BANK_CRISIS_EVENTS];
  const results = computeMatches('bank_crisis', STATE_ELEV_MID_HIGH, manyEvents);
  assertEquals(results.length <= 5, true);
});

Deno.test('6: 5件未満なら存在件数だけ返す', () => {
  // state_tags が state={vix:calm,oil:lo,rate:mid} の場合、svb は score=0 で全5件
  // pandemic なら covid_shock_2020 だけ（ただしBENK_CRISIS_EVENTSには含まれない）
  const onlyOne: ScoredEvent[] = [BANK_CRISIS_EVENTS[0]]; // eurozone のみ
  const results = computeMatches('bank_crisis', STATE_ELEV_MID_HIGH, onlyOne);
  assertEquals(results.length, 1);
});

Deno.test('7: context_snapshot 欠損イベント（全 null）は n_comp<2 で除外', () => {
  const withNull = [...BANK_CRISIS_EVENTS, ALL_NULL_EVENT];
  const results = computeMatches('bank_crisis', STATE_ELEV_MID_HIGH, withNull);
  const nullEventInResult = results.some((r) => r.event_id === 'no_snapshot_event');
  assertEquals(nullEventInResult, false, 'null-snapshot event should be excluded');
  assertEquals(results.length, 5); // BANK_CRISIS_EVENTS の 5 件のみ
});

Deno.test('8: 不正 cause_tag（存在しない）は空配列', () => {
  const results = computeMatches('nonexistent_tag', STATE_ELEV_MID_HIGH, BANK_CRISIS_EVENTS);
  assertEquals(results.length, 0);
});

Deno.test('9: RANK_EXCLUDED（oil_shock_1973）は常に除外', () => {
  const withExcluded = [OIL_SHOCK_1973, ...BANK_CRISIS_EVENTS];
  // oil_shock_1973 は middle_east タグも持つがコンテキストが空 → いずれにせよ除外
  const results = computeMatches('middle_east', STATE_ELEV_MID_HIGH, withExcluded);
  const oilShock = results.find((r) => r.event_id === 'oil_shock_1973');
  assertEquals(oilShock, undefined, 'oil_shock_1973 should be excluded by RANK_EXCLUDED');
});

Deno.test('10: rank フィールドが 1 始まりで連番', () => {
  const results = computeMatches('bank_crisis', STATE_ELEV_MID_HIGH, BANK_CRISIS_EVENTS);
  results.forEach((r, i) => assertEquals(r.rank, i + 1));
});

// ─── Section 3: Python alignment (state=stress/hi/low) ────────────────────────

// Python 出力（確認済み）:
//   1. lehman_shock_2008  score=2 (vix=stress,oil=hi 一致)
//   2. eurozone_crisis_2010 score=1 (oil=hi のみ; date 2010 > 1998)
//   3. ltcm_crisis_1998  score=1 (vix=stress のみ)
//   4. svb_collapse_2023  score=0
//   5. asian_crisis_1997  score=0

Deno.test('Python alignment: state=stress/hi/low', () => {
  const state = { vix: 'stress', oil: 'hi', rate: 'low' };
  const results = computeMatches('bank_crisis', state, BANK_CRISIS_EVENTS);
  assertEquals(results.length, 5);
  assertEquals(results[0].event_id, 'lehman_shock_2008');
  assertEquals(results[0].score, 2);
  assertEquals(results[1].event_id, 'eurozone_crisis_2010');
  assertEquals(results[1].score, 1);
  assertEquals(results[2].event_id, 'ltcm_crisis_1998');
  assertEquals(results[2].score, 1);
  // svb と asian の順: score=0,n_comp=3 同士 → date 降順 → svb(2023) > asian(1997)
  assertEquals(results[3].event_id, 'svb_collapse_2023');
  assertEquals(results[4].event_id, 'asian_crisis_1997');
});

// ─── Section 4: matched_axes / unmatched_axes の正確性 ───────────────────────

Deno.test('matched_axes が正確', () => {
  // svb: vix=19.1→elev, oil=76.7→mid, rate=avg(4.58,3.69)=4.135→high → 全一致
  const results = computeMatches('bank_crisis', STATE_ELEV_MID_HIGH, BANK_CRISIS_EVENTS);
  const svb = results.find((r) => r.event_id === 'svb_collapse_2023')!;
  assertEquals(svb.matched_axes.sort(), ['oil', 'rate', 'vix'].sort());
  assertEquals(svb.unmatched_axes, []);
});

Deno.test('unmatched_axes が正確（asian: vix,rate 一致; oil 不一致）', () => {
  // asian: vix=18.5→elev(○), oil=19.2→lo(✗mid), rate=avg(5.5,6.69)=6.095→high(○)
  const results = computeMatches('bank_crisis', STATE_ELEV_MID_HIGH, BANK_CRISIS_EVENTS);
  const asian = results.find((r) => r.event_id === 'asian_crisis_1997')!;
  assertEquals(asian.matched_axes.sort(), ['rate', 'vix'].sort());
  assertEquals(asian.unmatched_axes, ['oil']);
});
