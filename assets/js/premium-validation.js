/**
 * premium-validation.js — プレミアムAPIレスポンス検証・cause_tag 検証（純粋関数）
 *
 * ブラウザDOM・Supabase・CDN 依存なし。Deno test から直接 import 可能。
 *
 * セキュリティ:
 *   - validateMatchResponse は APIレスポンスの構造検証のみ行う。
 *     認可の判断は行わない。
 *   - sanitizeCauseTag は allowedSet（JSONから取得した値）に存在する場合のみ返す。
 *     URLパラメータや任意文字列を直接 APIへ渡すことを防ぐ。
 */

/**
 * get-similar-matches APIのレスポンスを検証する。
 *
 * 有効な条件:
 *   - data はオブジェクト（null/配列除く）
 *   - data.matches は配列で長さ 0〜5
 *   - 各要素は event_id(非空文字列), name(文字列), date(文字列),
 *     score(数値), matched_axes(配列), unmatched_axes(配列),
 *     reactions(オブジェクト, null/配列除く) を持つ
 *
 * @param {unknown} data - APIレスポンスbody
 * @returns {boolean}
 */
export function validateMatchResponse(data) {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return false;
  if (!Array.isArray(data.matches)) return false;
  if (data.matches.length > 5) return false;
  for (const m of data.matches) {
    if (typeof m !== 'object' || m === null || Array.isArray(m)) return false;
    if (typeof m.event_id !== 'string' || m.event_id.length === 0) return false;
    if (typeof m.name !== 'string') return false;
    if (typeof m.date !== 'string') return false;
    if (typeof m.score !== 'number') return false;
    if (!Array.isArray(m.matched_axes)) return false;
    if (!Array.isArray(m.unmatched_axes)) return false;
    if (typeof m.reactions !== 'object' || m.reactions === null || Array.isArray(m.reactions)) {
      return false;
    }
  }
  return true;
}

/**
 * cause_tag が allowedSet に含まれる場合のみ返す。含まれない場合は null。
 *
 * URLパラメータや任意の文字列を allowedSet（JSONから構築）でフィルタリングし、
 * API に送信できる値だけを返す。
 *
 * @param {unknown} cause - 検証対象の cause_tag 文字列
 * @param {Set<string>} allowedSet - free_top_match のキーから構築した許可セット
 * @returns {string|null}
 */
export function sanitizeCauseTag(cause, allowedSet) {
  if (typeof cause !== 'string' || cause.length === 0) return null;
  if (!(allowedSet instanceof Set)) return null;
  return allowedSet.has(cause) ? cause : null;
}
