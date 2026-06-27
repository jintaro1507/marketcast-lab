/**
 * timeline.ts — Reaction Timeline 共通ロジック
 *
 * 責務:
 *   - 資産別の価格変化方向判定（up/down/flat/na）
 *   - 中期反転検出（d1・d30の符号が逆かつ双方の絶対値≥0.5%）
 *   - 複数Edge Functionから再利用可能な純粋関数を提供
 *
 * このロジックはサーバー側でのみ実行する。
 * クライアントはAPIレスポンスのdirections値を表示に利用する。
 *
 * 実行コマンド（ユニットテスト）:
 *   deno test supabase/functions/_shared/timeline_test.ts
 */

/** 方向判定結果。クライアントへの返却値は4種のみ。 */
export type Direction = 'up' | 'down' | 'flat' | 'na';

/** 時点キー */
export const HORIZONS = ['d1', 'd7', 'd30', 'd90'] as const;
export type Horizon = typeof HORIZONS[number];

/** 資産表示順 */
export const ASSET_ORDER = ['wti', 'gold', 'sp500', 'ust10y', 'usdjpy', 'vix'] as const;

/**
 * 中期反転判定のノイズ除外閾値（%）。
 * d1・d30の両方がこの絶対値以上の場合のみ反転を検出する。
 * 根拠: ドル円d1中央値≒0.40%。0.5%未満の変動は方向差として不十分。
 */
export const REVERSAL_THRESHOLD = 0.5;

/** 免責文言（サーバー側で定数管理し、APIレスポンスに含める） */
export const DISCLAIMER =
  'これらの方向表示は、過去のイベント後における各資産の価格変化の方向を記録したものです。' +
  '売買推奨、投資シグナル、将来の値動きの予測ではありません。';

/** 資産別のTimeline計算結果 */
export interface AssetTimeline {
  asset_key: string;
  label: string;
  asset: string;
  restricted: boolean;
  status: string;
  base_date: string | null;
  changes: Record<string, number | null>;
  changes_pt: Record<string, number | null> | null;
  directions: Record<Horizon, Direction>;
  mid_term_reversal: boolean;
}

/** computeEventTimeline に渡す資産データの最低限のインタフェース */
export interface ReactionAssetLike {
  label?: string | null;
  asset?: string | null;
  restricted?: boolean | null;
  status?: string | null;
  base_date?: string | null;
  changes?: Record<string, unknown> | null;
  changes_pt?: Record<string, unknown> | null;
}

/** computeEventTimeline に渡すイベントの最低限のインタフェース */
export interface EventLike {
  reactions?: Record<string, ReactionAssetLike> | null;
}

// ─── 純粋関数（ユニットテスト対象） ───────────────────────────────────────────

/**
 * 変化率の値から方向を判定する。
 *
 * value > 0  → 'up'
 * value < 0  → 'down'
 * value == 0 → 'flat'
 * null / undefined / NaN / ±Infinity → 'na'
 */
export function directionOf(value: unknown): Direction {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'na';
  if (value > 0) return 'up';
  if (value < 0) return 'down';
  return 'flat';
}

/**
 * d1（初動）と d30（中期）の反転を検出する。
 *
 * 以下の全条件を満たす場合に true:
 *   1. d1 が有限数
 *   2. d30 が有限数
 *   3. |d1| >= REVERSAL_THRESHOLD
 *   4. |d30| >= REVERSAL_THRESHOLD
 *   5. d1 と d30 の符号が逆
 *
 * 設計意図: ノイズ除外閾値により、変動幅が小さい資産での誤検出を防ぐ。
 * これは売買シグナルではなく過去の反応パターン分類。
 */
export function detectReversal(d1: unknown, d30: unknown): boolean {
  if (typeof d1 !== 'number' || !Number.isFinite(d1)) return false;
  if (typeof d30 !== 'number' || !Number.isFinite(d30)) return false;
  if (Math.abs(d1) < REVERSAL_THRESHOLD) return false;
  if (Math.abs(d30) < REVERSAL_THRESHOLD) return false;
  return (d1 > 0 && d30 < 0) || (d1 < 0 && d30 > 0);
}

/**
 * 資産データから AssetTimeline を生成する。
 * changes の各値は非数値・非有限数の場合は null として扱う。
 */
export function computeAssetTimeline(
  assetKey: string,
  assetData: ReactionAssetLike,
): AssetTimeline {
  const rawChanges = assetData.changes ?? {};
  const rawChangesPt = assetData.changes_pt ?? null;

  const changes: Record<string, number | null> = {};
  for (const h of HORIZONS) {
    const v = rawChanges[h];
    changes[h] = (typeof v === 'number' && Number.isFinite(v)) ? v : null;
  }

  let changes_pt: Record<string, number | null> | null = null;
  if (rawChangesPt && typeof rawChangesPt === 'object') {
    changes_pt = {};
    for (const h of HORIZONS) {
      const v = rawChangesPt[h];
      changes_pt[h] = (typeof v === 'number' && Number.isFinite(v)) ? v : null;
    }
  }

  const directions = {} as Record<Horizon, Direction>;
  for (const h of HORIZONS) {
    directions[h] = directionOf(changes[h]);
  }

  return {
    asset_key: assetKey,
    label: (typeof assetData.label === 'string' && assetData.label) ? assetData.label : assetKey,
    asset: (typeof assetData.asset === 'string') ? assetData.asset : '',
    restricted: assetData.restricted === true,
    status: (typeof assetData.status === 'string') ? assetData.status : 'unknown',
    base_date: (typeof assetData.base_date === 'string') ? assetData.base_date : null,
    changes,
    changes_pt,
    directions,
    mid_term_reversal: detectReversal(changes['d1'], changes['d30']),
  };
}

/**
 * イベントデータから全資産のTimeline配列を生成する。
 *
 * - ASSET_ORDER 順で処理する
 * - status が 'ok' の資産のみ含む
 * - 個別資産の処理エラーはスキップして続行する
 */
export function computeEventTimeline(event: EventLike): AssetTimeline[] {
  const reactions = event.reactions;
  if (!reactions || typeof reactions !== 'object') return [];

  const assets: AssetTimeline[] = [];
  for (const assetKey of ASSET_ORDER) {
    const assetData = reactions[assetKey];
    if (!assetData || typeof assetData !== 'object') continue;
    if (assetData.status !== 'ok') continue;
    try {
      assets.push(computeAssetTimeline(assetKey, assetData));
    } catch (e) {
      const safeMsg = e instanceof Error ? e.message.slice(0, 80) : String(e).slice(0, 80);
      console.warn('[timeline] asset skipped', { asset_key: assetKey, reason: safeMsg });
    }
  }
  return assets;
}
