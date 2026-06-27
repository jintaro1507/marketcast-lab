/**
 * timeline_test.ts — directionOf / detectReversal / computeEventTimeline のユニットテスト
 *
 * 実行コマンド:
 *   deno test supabase/functions/_shared/timeline_test.ts
 */

import { assertEquals } from 'jsr:@std/assert@1';
import {
  directionOf,
  detectReversal,
  computeAssetTimeline,
  computeEventTimeline,
} from './timeline.ts';

// ─── Section 1: directionOf ────────────────────────────────────────────────

Deno.test('directionOf: 正値 → up', () => assertEquals(directionOf(8.3), 'up'));
Deno.test('directionOf: 負値 → down', () => assertEquals(directionOf(-12.1), 'down'));
Deno.test('directionOf: 0 → flat', () => assertEquals(directionOf(0), 'flat'));
Deno.test('directionOf: null → na', () => assertEquals(directionOf(null), 'na'));
Deno.test('directionOf: undefined → na', () => assertEquals(directionOf(undefined), 'na'));
Deno.test('directionOf: NaN → na', () => assertEquals(directionOf(NaN), 'na'));
Deno.test('directionOf: +Infinity → na', () => assertEquals(directionOf(Infinity), 'na'));
Deno.test('directionOf: -Infinity → na', () => assertEquals(directionOf(-Infinity), 'na'));
Deno.test('directionOf: 文字列 → na', () => assertEquals(directionOf('5.0'), 'na'));
Deno.test('directionOf: 極小正値(0.001) → up', () => assertEquals(directionOf(0.001), 'up'));
Deno.test('directionOf: 極小負値(-0.001) → down', () => assertEquals(directionOf(-0.001), 'down'));
Deno.test('directionOf: 大きな正値(285.0) → up', () => assertEquals(directionOf(285.0), 'up'));

// ─── Section 2: detectReversal ────────────────────────────────────────────

Deno.test('detectReversal: d1正/d30負、双方>=0.5 → true (saudi/vix パターン)', () =>
  assertEquals(detectReversal(8.3, -12.1), true));

Deno.test('detectReversal: d1負/d30正、双方>=0.5 → true (russia/ust10y パターン)', () =>
  assertEquals(detectReversal(-2.1, 18.4), true));

Deno.test('detectReversal: d1=0.6/d30=-0.6、双方>=0.5 → true', () =>
  assertEquals(detectReversal(0.6, -0.6), true));

Deno.test('detectReversal: 境界値 d1=0.5/d30=-0.5 → true', () =>
  assertEquals(detectReversal(0.5, -0.5), true));

Deno.test('detectReversal: d1=0.49/d30=-1.0、d1<0.5 → false', () =>
  assertEquals(detectReversal(0.49, -1.0), false));

Deno.test('detectReversal: d1=1.0/d30=-0.49、d30<0.5 → false', () =>
  assertEquals(detectReversal(1.0, -0.49), false));

Deno.test('detectReversal: d1=0.4/d30=-0.8（ドル円d1相当）→ false', () =>
  assertEquals(detectReversal(0.4, -0.8), false));

Deno.test('detectReversal: 同符号正 → false', () =>
  assertEquals(detectReversal(2.0, 3.0), false));

Deno.test('detectReversal: 同符号負 → false', () =>
  assertEquals(detectReversal(-2.0, -3.0), false));

Deno.test('detectReversal: d1=null → false', () =>
  assertEquals(detectReversal(null, -5.0), false));

Deno.test('detectReversal: d30=null → false', () =>
  assertEquals(detectReversal(5.0, null), false));

Deno.test('detectReversal: 両方null → false', () =>
  assertEquals(detectReversal(null, null), false));

Deno.test('detectReversal: undefined → false', () =>
  assertEquals(detectReversal(undefined, undefined), false));

Deno.test('detectReversal: NaN → false', () =>
  assertEquals(detectReversal(NaN, -5.0), false));

Deno.test('detectReversal: +Infinity → false', () =>
  assertEquals(detectReversal(Infinity, -5.0), false));

// ─── Section 3: computeAssetTimeline ─────────────────────────────────────

Deno.test('computeAssetTimeline: 全4時点の方向判定', () => {
  const result = computeAssetTimeline('vix', {
    label: 'VIX',
    asset: 'vix',
    restricted: false,
    status: 'ok',
    base_date: '2019-09-13',
    changes: { d1: 8.3, d7: -4.2, d30: -12.1, d90: -18.5 },
  });
  assertEquals(result.asset_key, 'vix');
  assertEquals(result.label, 'VIX');
  assertEquals(result.directions.d1, 'up');
  assertEquals(result.directions.d7, 'down');
  assertEquals(result.directions.d30, 'down');
  assertEquals(result.directions.d90, 'down');
  assertEquals(result.mid_term_reversal, true);
});

Deno.test('computeAssetTimeline: 同方向（反転なし）', () => {
  const result = computeAssetTimeline('wti', {
    label: 'WTI原油',
    asset: 'oil',
    restricted: false,
    status: 'ok',
    changes: { d1: 14.7, d7: 6.2, d30: 1.8, d90: -3.4 },
  });
  assertEquals(result.directions.d1, 'up');
  assertEquals(result.directions.d30, 'up');
  assertEquals(result.mid_term_reversal, false);
});

Deno.test('computeAssetTimeline: changes_ptが存在する場合に保持される', () => {
  const result = computeAssetTimeline('ust10y', {
    label: '米10年債',
    asset: 'bond',
    restricted: false,
    status: 'ok',
    changes: { d1: -2.1, d7: 1.2, d30: 18.4, d90: 42.1 },
    changes_pt: { d1: -0.02, d7: 0.01, d30: 0.18, d90: 0.42 },
  });
  assertEquals(result.changes_pt !== null, true);
  assertEquals(result.changes_pt?.d1, -0.02);
  assertEquals(result.mid_term_reversal, true);
});

Deno.test('computeAssetTimeline: NaN値はnullに正規化', () => {
  const result = computeAssetTimeline('wti', {
    status: 'ok',
    changes: { d1: NaN, d7: 1.0, d30: null, d90: undefined },
  });
  assertEquals(result.changes.d1, null);
  assertEquals(result.changes.d7, 1.0);
  assertEquals(result.changes.d30, null);
  assertEquals(result.changes.d90, null);
  assertEquals(result.directions.d1, 'na');
  assertEquals(result.directions.d7, 'up');
});

// ─── Section 4: computeEventTimeline ─────────────────────────────────────

const SAMPLE_EVENT = {
  reactions: {
    wti: {
      label: 'WTI原油', asset: 'oil', restricted: false, status: 'ok',
      changes: { d1: 14.7, d7: 6.2, d30: 1.8, d90: -3.4 },
    },
    gold: {
      label: '金', asset: 'gold', restricted: true, status: 'ok',
      changes: { d1: 1.2, d7: -0.3, d30: 0.9, d90: 3.1 },
    },
    sp500: {
      label: 'S&P500', asset: 'equity', restricted: true, status: 'no_data',
      changes: {},
    },
    ust10y: {
      label: '米10年債', asset: 'bond', restricted: false, status: 'ok',
      changes: { d1: 1.4, d7: -2.1, d30: -5.2, d90: -8.1 },
    },
    usdjpy: {
      label: 'ドル円', asset: 'fx', restricted: false, status: 'ok',
      changes: { d1: 0.2, d7: 0.4, d30: -0.3, d90: -0.8 },
    },
    vix: {
      label: 'VIX', asset: 'vix', restricted: false, status: 'ok',
      changes: { d1: 8.3, d7: -4.2, d30: -12.1, d90: -18.5 },
    },
  },
};

Deno.test('computeEventTimeline: ASSET_ORDER順で返す（no_dataは除外）', () => {
  const result = computeEventTimeline(SAMPLE_EVENT);
  const keys = result.map(a => a.asset_key);
  assertEquals(keys, ['wti', 'gold', 'ust10y', 'usdjpy', 'vix']);
});

Deno.test('computeEventTimeline: no_dataはスキップする', () => {
  const result = computeEventTimeline(SAMPLE_EVENT);
  assertEquals(result.some(a => a.asset_key === 'sp500'), false);
});

Deno.test('computeEventTimeline: reactionsがnullの場合は空配列', () => {
  assertEquals(computeEventTimeline({ reactions: null }), []);
});

Deno.test('computeEventTimeline: reactionsが空オブジェクトの場合は空配列', () => {
  assertEquals(computeEventTimeline({ reactions: {} }), []);
});

Deno.test('computeEventTimeline: vixの方向が全時点正しい', () => {
  const result = computeEventTimeline(SAMPLE_EVENT);
  const vix = result.find(a => a.asset_key === 'vix');
  assertEquals(vix?.directions.d1, 'up');
  assertEquals(vix?.directions.d7, 'down');
  assertEquals(vix?.directions.d30, 'down');
  assertEquals(vix?.directions.d90, 'down');
  assertEquals(vix?.mid_term_reversal, true);
});

Deno.test('computeEventTimeline: ドル円の中期反転はfalse（閾値未満）', () => {
  const result = computeEventTimeline(SAMPLE_EVENT);
  const usdjpy = result.find(a => a.asset_key === 'usdjpy');
  assertEquals(usdjpy?.mid_term_reversal, false);
});

Deno.test('computeEventTimeline: restricted=trueの資産（gold）も含まれる', () => {
  const result = computeEventTimeline(SAMPLE_EVENT);
  const gold = result.find(a => a.asset_key === 'gold');
  assertEquals(gold !== undefined, true);
  assertEquals(gold?.restricted, true);
});

// ─── Section 5: 例外ハンドリングと0件ケース ───────────────────────────────

Deno.test('computeEventTimeline: 1資産のchangesアクセスが例外を投げても他の正常資産は返る', () => {
  // changesプロパティへのアクセスが例外を投げる資産を混入させる
  const evilReactions: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(SAMPLE_EVENT.reactions)) {
    evilReactions[k] = v;
  }
  Object.defineProperty(evilReactions, 'wti', {
    get() { throw new Error('simulated read error'); },
    enumerable: true,
  });
  // wti は例外でスキップされるが、他の ok 資産は返る
  const result = computeEventTimeline({ reactions: evilReactions as never });
  assertEquals(result.some(a => a.asset_key === 'wti'), false);
  // gold, ust10y, usdjpy, vix (status='ok') は残る
  assertEquals(result.some(a => a.asset_key === 'gold'), true);
  assertEquals(result.some(a => a.asset_key === 'vix'), true);
});

Deno.test('computeEventTimeline: 全資産のstatusがok以外のとき空配列を返す', () => {
  const noOkEvent = {
    reactions: {
      wti:   { status: 'no_data', changes: {} },
      gold:  { status: 'error',   changes: {} },
      sp500: { status: 'no_data', changes: {} },
      ust10y:{ status: 'no_data', changes: {} },
      usdjpy:{ status: 'no_data', changes: {} },
      vix:   { status: 'no_data', changes: {} },
    },
  };
  const result = computeEventTimeline(noOkEvent);
  assertEquals(result.length, 0);
});

// 注: 「有効資産0件を get-event-reaction-timeline が正常成功扱いしない」および
// 「Matcher enrichment で 0件のtimeline が null になる」は Edge Function レベルの
// 挙動であり、Deno.serve をモックなしで単体テストするには統合テスト環境が必要。
// それぞれ index.ts の実装で対処済み:
//   get-event-reaction-timeline/index.ts: assets.length === 0 → 503
//   get-similar-matches/index.ts: computed.length === 0 → timeline = null
//
// 以下のテストは、その前提となる「computeEventTimeline が0件を返すこと」を確認する。

Deno.test('computeEventTimeline: 0件の場合length===0（上位でnull/503にするための前提）', () => {
  const empty = computeEventTimeline({ reactions: {} });
  assertEquals(empty.length, 0);
  // 上位ロジック: get-event-reaction-timeline では 503, get-similar-matches では null
});

Deno.test('computeEventTimeline: 全資産が例外の場合も空配列を返しcrashしない', () => {
  const allEvilReactions = {};
  for (const key of ['wti', 'gold', 'sp500', 'ust10y', 'usdjpy', 'vix']) {
    Object.defineProperty(allEvilReactions, key, {
      get() { throw new Error('all broken'); },
      enumerable: true,
    });
  }
  // crashせず空配列を返すことを確認
  const result = computeEventTimeline({ reactions: allEvilReactions as never });
  assertEquals(result.length, 0);
});
