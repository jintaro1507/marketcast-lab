/**
 * run_weekly_matcher.ts — Deno CLI wrapper for matching.ts + timeline.ts
 *
 * stdin:  JSON { cause_tag, state_tags, events, top_n? }
 * stdout: JSON { matches: [...] } — directions only, no raw values
 * stderr: errors and warnings only
 *
 * 実行例:
 *   echo '{"cause_tag":"war","state_tags":{"vix":"stress","oil":"hi","rate":"mid"},"events":[...]}' \
 *     | deno run --allow-read scripts/run_weekly_matcher.ts
 */

import { computeMatches, type ScoredEvent } from '../supabase/functions/get-similar-matches/matching.ts';
import { computeEventTimeline } from '../supabase/functions/_shared/timeline.ts';

// ─── stdin 読み込み ─────────────────────────────────────────────────────────

const parts: string[] = [];
for await (const chunk of Deno.stdin.readable.pipeThrough(new TextDecoderStream())) {
  parts.push(chunk);
}
const stdinText = parts.join('');

let input: Record<string, unknown>;
try {
  input = JSON.parse(stdinText);
} catch (e) {
  console.error(`[run_weekly_matcher] stdin JSON parse error: ${(e as Error).message}`);
  Deno.exit(1);
}

// ─── 入力検証 ───────────────────────────────────────────────────────────────

const { cause_tag, state_tags, events, top_n = 5 } = input;

if (typeof cause_tag !== 'string' || cause_tag.trim().length === 0) {
  console.error('[run_weekly_matcher] cause_tag must be a non-empty string');
  Deno.exit(1);
}

if (!state_tags || typeof state_tags !== 'object' || Array.isArray(state_tags)) {
  console.error('[run_weekly_matcher] state_tags must be an object');
  Deno.exit(1);
}

if (!Array.isArray(events)) {
  console.error('[run_weekly_matcher] events must be an array');
  Deno.exit(1);
}

const typedStateTags = state_tags as Record<string, string>;
const typedEvents = events as ScoredEvent[];
const topN = typeof top_n === 'number' ? top_n : 5;

// ─── マッチング実行 ─────────────────────────────────────────────────────────

const matches = computeMatches(cause_tag, typedStateTags, typedEvents, topN);

// ─── Timeline 生成（方向のみ、生値なし） ────────────────────────────────────

// event_id → cause_tags のマップ（テーマ選定用）
const eventCauseTags = new Map<string, string[]>();
for (const ev of typedEvents) {
  if (ev.id) {
    eventCauseTags.set(ev.id, ev.cause_tags ?? []);
  }
}

const enriched = matches.map((m) => {
  const timelines: Record<string, { d1: string; d7: string; d30: string; d90: string; mid_term_reversal: boolean }> = {};

  try {
    const computed = computeEventTimeline({ reactions: m.reactions });
    for (const asset of computed) {
      timelines[asset.asset_key] = {
        d1: asset.directions.d1,
        d7: asset.directions.d7,
        d30: asset.directions.d30,
        d90: asset.directions.d90,
        mid_term_reversal: asset.mid_term_reversal,
      };
    }
  } catch (e) {
    // Timeline 生成失敗はマッチング結果全体を壊さない
    console.error(`[run_weekly_matcher] timeline error for ${m.event_id}: ${(e as Error).message}`);
  }

  return {
    rank: m.rank,
    event_id: m.event_id,
    event_name: m.name,
    event_date: m.date,
    score: m.score,
    matched_axes: m.matched_axes,
    unmatched_axes: m.unmatched_axes,
    cause_tags: eventCauseTags.get(m.event_id) ?? [],
    why_reaction: m.why_reaction,
    key_insight: m.key_insight,
    timelines,
  };
});

// ─── stdout に JSON のみ出力 ────────────────────────────────────────────────
// stdout にはこの1行のみ書く。ログは stderr に限定。

console.log(JSON.stringify({ matches: enriched }));
