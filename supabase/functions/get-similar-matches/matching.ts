/**
 * matching.ts — 類似局面スコアリング純粋関数
 *
 * scripts/calculate_event_reactions.py の以下の関数を TypeScript に移植:
 *   classify_vix / classify_oil / classify_rate / build_market_state_tags / _top1_for_cause
 *
 * 入力値の分類閾値・tie-break規則・欠損処理はすべて Python 実装に準拠する。
 * 推測での差異実装はしない。
 */

// ─── 型定義 ──────────────────────────────────────────────────────────────────

export interface ContextSnapshot {
  vix_level?: number | null;
  oil_price_wti?: number | null;
  fed_funds_rate?: number | null;
  ust10y_yield?: number | null;
  cpi_yoy?: number | null;
}

export interface ReactionAsset {
  label: string;
  asset: string;
  restricted: boolean;
  status: string;
  base_date?: string | null;
  changes: Record<string, number | null>;
  changes_pt?: Record<string, number | null>;
}

export interface ScoredEvent {
  id: string;
  name: string;
  date: string;
  cause_tags?: string[];
  context_snapshot?: ContextSnapshot | null;
  reactions?: Record<string, ReactionAsset>;
  similarity_reason?: string | null;
  why_reaction?: string | null;
  key_insight?: string | null;
}

export interface MatchResult {
  rank: number;
  event_id: string;
  name: string;
  date: string;
  score: number;
  matched_axes: string[];
  unmatched_axes: string[];
  context_snapshot: ContextSnapshot;
  reactions: Record<string, ReactionAsset>;
  similarity_reason: string | null;
  why_reaction: string | null;
  key_insight: string | null;
}

// ─── 定数 ────────────────────────────────────────────────────────────────────

// Python: RANK_EXCLUDED = {"oil_shock_1973"}
// 参照用イベント（コンテキストデータが不完全で比較対象外）
const RANK_EXCLUDED = new Set<string>(['oil_shock_1973']);

export const TOP_N = 5;

// ─── 分類関数（Python の classify_* を完全移植） ─────────────────────────────

/**
 * VIX 水準を帯タグ化。Python: classify_vix()
 * null/undefined → null
 */
export function classifyVix(value: number | null | undefined): string | null {
  if (value == null) return null;
  if (value < 15) return 'calm';
  if (value < 25) return 'elev';
  if (value < 40) return 'stress';
  return 'panic';
}

/**
 * WTI 原油水準を帯タグ化。Python: classify_oil()
 * null/undefined → null
 */
export function classifyOil(value: number | null | undefined): string | null {
  if (value == null) return null;
  if (value < 40) return 'lo';
  if (value < 80) return 'mid';
  return 'hi';
}

/**
 * FF 金利と 10 年債利回りの単純平均で金利環境を帯タグ化。Python: classify_rate()
 * 片方欠損時は存在する値のみ利用。両方欠損 → null。
 */
export function classifyRate(
  ff: number | null | undefined,
  ust10y: number | null | undefined,
): string | null {
  const vals = ([ff, ust10y] as (number | null | undefined)[])
    .filter((v): v is number => v != null);
  if (vals.length === 0) return null;
  const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
  if (avg < 2) return 'low';
  if (avg < 4) return 'mid';
  return 'high';
}

/**
 * 4 指標値から market_state_tags dict を生成。Python: build_market_state_tags()
 * タグなし軸はキー自体を含めない。
 */
export function buildMarketStateTags(
  vix: number | null | undefined,
  oil: number | null | undefined,
  ff: number | null | undefined,
  ust10y: number | null | undefined,
): Record<string, string> {
  const tags: Record<string, string> = {};
  const v = classifyVix(vix);
  if (v !== null) tags.vix = v;
  const o = classifyOil(oil);
  if (o !== null) tags.oil = o;
  const r = classifyRate(ff, ust10y);
  if (r !== null) tags.rate = r;
  return tags;
}

// ─── スコアリング・ランキング ──────────────────────────────────────────────────

/**
 * Python の _date_key(d) を移植。
 * YYYY-MM-DD 文字列を整数化して負値にする（降順ソート用）。
 * 解析失敗時は 0。
 */
function dateKey(d: string): number {
  const n = parseInt(d.replace(/-/g, ''), 10);
  return isNaN(n) ? 0 : -n;
}

/**
 * 指定 cause_tag のイベント群を現在の market_state_tags と照合し、
 * top5 のマッチ結果を返す。
 *
 * Python の _top1_for_cause() と同じスコアリング・ソート規則を適用し、
 * top N 件を返すよう拡張したもの。
 *
 * ソート基準（Python 準拠）:
 *   1. n_match 降順（一致軸の数が多いほど上位）
 *   2. n_comp 降順（比較可能軸の数が多いほど上位）
 *   3. date 降順（新しいイベントほど上位 ── tie-break）
 *
 * 除外条件（Python 準拠）:
 *   - RANK_EXCLUDED に含まれるイベント
 *   - 比較可能軸が 2 未満のイベント（context_snapshot が不完全なケースで発生）
 */
export function computeMatches(
  causeTag: string,
  stateTags: Record<string, string>,
  events: ScoredEvent[],
  topN: number = TOP_N,
): MatchResult[] {
  // 対象 cause_tag を持ち、RANK_EXCLUDED でないイベントを抽出
  const group = events.filter(
    (e) =>
      (e.cause_tags ?? []).includes(causeTag) &&
      !RANK_EXCLUDED.has(e.id),
  );

  if (group.length === 0) return [];

  type Candidate = {
    nMatch: number;
    nComp: number;
    date: string;
    matchedAxes: string[];
    unmatchedAxes: string[];
    ev: ScoredEvent;
  };

  const candidates: Candidate[] = [];

  for (const ev of group) {
    const cs = ev.context_snapshot ?? {};
    const evTags = buildMarketStateTags(
      cs.vix_level,
      cs.oil_price_wti,
      cs.fed_funds_rate,
      cs.ust10y_yield,
    );

    // 現在環境と過去イベントの両者に存在する軸のみ比較
    const stateAxes = Object.keys(stateTags);
    const commonAxes = stateAxes.filter((ax) => ax in evTags);

    // Python: 比較可能軸が 2 未満は除外
    if (commonAxes.length < 2) continue;

    const matchedAxes = commonAxes.filter((ax) => stateTags[ax] === evTags[ax]);
    const unmatchedAxes = commonAxes.filter((ax) => stateTags[ax] !== evTags[ax]);

    candidates.push({
      nMatch: matchedAxes.length,
      nComp: commonAxes.length,
      date: ev.date,
      matchedAxes,
      unmatchedAxes,
      ev,
    });
  }

  if (candidates.length === 0) return [];

  // Python: scored.sort(key=lambda x: (-x[0], -x[1], _date_key(x[2])))
  candidates.sort((a, b) => {
    if (a.nMatch !== b.nMatch) return b.nMatch - a.nMatch;
    if (a.nComp !== b.nComp) return b.nComp - a.nComp;
    // dateKey は負値。小さい方 = より負 = より新しい日付
    return dateKey(a.date) - dateKey(b.date);
  });

  return candidates.slice(0, topN).map((c, i) => ({
    rank: i + 1,
    event_id: c.ev.id,
    name: c.ev.name,
    date: c.ev.date,
    score: c.nMatch,
    matched_axes: c.matchedAxes,
    unmatched_axes: c.unmatchedAxes,
    context_snapshot: c.ev.context_snapshot ?? {},
    reactions: c.ev.reactions ?? {},
    similarity_reason: c.ev.similarity_reason ?? null,
    why_reaction: c.ev.why_reaction ?? null,
    key_insight: c.ev.key_insight ?? null,
  }));
}
