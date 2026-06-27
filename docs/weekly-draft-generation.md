# Weekly Marketcast Draft Generation (W2-2)

## 概要

`generate_weekly_draft.py` はルールベースでWeekly Marketcastのドラフト（`free_teaser` + `paid_body`）を生成するスクリプトです。AI APIは使用しません。

## 入力

| ファイル | パス | 説明 |
|---|---|---|
| weekly_changes.json | `data/weekly/YYYY-WXX_changes.json` | W2-1が生成した週次diffデータ |
| current_context_public.json | `data/weekly/current_context_public.json` | CI生成。VIX/WTI絶対値・market_state_tags |
| event_reactions.json | `data/event_reactions.json` | 過去イベントのMatcherデータ |
| group_metadata.json | `data/group_metadata.json` | テーマグループメタデータ |

`current_context_public.json` はリポジトリに含まれません（CIが生成）。ローカル実行では `--context-json` オプションで指定してください。

## 出力

ドラフトは `~/.local/share/marketcast-lab/drafts/YYYY-WXX_draft.json` に保存されます（ディレクトリ 700、ファイル 600）。

`free_teaser` と `paid_body` のスキーマは `supabase/functions/_shared/schemas/` を参照してください。

## 実行方法

```bash
# 通常実行
python scripts/generate_weekly_draft.py --week-id 2026-W26

# dry-run（ファイル保存しない）
python scripts/generate_weekly_draft.py --week-id 2026-W26 --dry-run

# 入力JSONを明示指定
python scripts/generate_weekly_draft.py \
  --week-id 2026-W26 \
  --input-json data/weekly/2026-W26_changes.json \
  --context-json data/weekly/current_context_public.json
```

### オプション

| オプション | 説明 |
|---|---|
| `--week-id` | 週識別子（例: `2026-W26`）。必須 |
| `--input-json` | weekly_changes.json のパス。省略時は `data/weekly/{week_id}_changes.json` |
| `--context-json` | current_context_public.json のパス。省略時は `data/weekly/current_context_public.json` |
| `--dry-run` | ドラフトを保存しない。検証・HARD確認に使用 |

## HARD エラーと WARN

### HARD エラー（生成停止）

- 生成テキスト（summary、obs_points）に禁止表現を検出
- `data_completeness` < 2（データ不足）
- 注目テーマが0件
- free_teaserにpaid情報が漏洩（score、timelines等）
- JSON Schema バリデーション失敗
- Matcher実行失敗

HARDエラーがある場合、ドラフトはファイルに保存されず exit 1 で終了します。

### WARN（生成継続）

- 承認済みメタデータ（group_metadata.jsonのsummary/interpretation_caveat）に禁止表現を含む
- similar_eventsのwhy_reaction / key_insightが未設定

WARNはドラフト内の `warnings` フィールドに記録されます。

## Matcher と Timeline の再利用

Edge Functionと同一の `matching.ts` / `timeline.ts` をDeno CLI経由（`scripts/run_weekly_matcher.ts`）で呼び出します。Pythonへの移植は行いません。

Matcherは `event_reactions.json` に存在する全 cause_tag に対して実行し、event_id で重複排除後、score降順・日付降順で再ランキングします。

## restricted 非露出

restricted 資産（gold、sp500）の `end_value` は常に `null` です。生値はドラフトに含まれません。

free_teaser には paid_body の情報（score、matched_axes、timelines等）を含めません。

## 禁止表現チェック

生成テキストに以下のような投資勧誘・予測表現が含まれる場合はHARDエラーになります（代表例）:

- 「上昇する見通し」「下落する見込み」「狙い目」「推奨」「割安」「買い場」
- 「絶好の」「底値」「高値」「確実」等

## スキーマ

- `free_teaser`: `supabase/functions/_shared/schemas/weekly_free_teaser_schema.json`
- `paid_body`: `supabase/functions/_shared/schemas/weekly_paid_body_schema.json`

JSON Schema Draft 2020-12 / `additionalProperties: false` で厳密バリデーションします。

## hash

`teaser_hash` と `paid_body_hash` は SHA-256 canonical JSON hash（`sort_keys=True, separators=(',',':')` のUTF-8エンコード）です。64文字の小文字16進数文字列です。

## 異常時の停止

| 状況 | 挙動 |
|---|---|
| `current_context_public.json` が存在しない | `MatcherError` → exit 1 |
| `market_state_tags` が空 | `MatcherError` → exit 1 |
| `data_completeness` < 2 | HARDエラー → exit 1 |
| Deno が利用できない | `MatcherError` → exit 1 |
| HARDエラー | ドラフト非保存 → exit 1 |
| 既存ドラフトが存在する | 上書き確認プロンプト（`--dry-run` は確認なし） |

## W2-2 で実装しないもの

- `weekly_reports` テーブルへの INSERT
- ドラフト承認フロー
- Pages JSON 生成
- Edge Function・HTML変更
- cron・メール配信
- AI API 呼び出し
