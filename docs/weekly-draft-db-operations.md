# Weekly Marketcast Draft DB 保存操作（W2-3）

## 概要

`save_weekly_report_draft.py` はローカル draft ファイルを検証し、`weekly_reports` テーブルへ `status='draft'` として保存するスクリプトです。

## ローカル draft から DB draft への保存

```bash
# 通常実行（確認プロンプトあり）
python scripts/save_weekly_report_draft.py --week-id 2026-W26

# draft ファイルを明示指定
python scripts/save_weekly_report_draft.py \
  --week-id 2026-W26 \
  --draft-path /path/to/draft.json

# dry-run（DB 書き込みなし）
python scripts/save_weekly_report_draft.py --week-id 2026-W26 --dry-run
```

デフォルトの draft 読み込み先: `~/.local/share/marketcast-lab/drafts/YYYY-WXX_draft.json`

## status=draft 制約

`weekly_reports` の DB CHECK 制約により、`status='draft'` の行では以下がすべて NULL である必要があります。

| カラム | draft 時の値 |
|---|---|
| `reviewed_at` | NULL |
| `reviewed_by` | NULL |
| `published_at` | NULL |
| `withdrawn_at` | NULL |
| `withdrawal_reason` | NULL |
| `teaser_hash` | NULL |
| `paid_body_hash` | NULL |

## hash を DB にまだ保存しない理由

ローカル draft ファイルには `teaser_hash` / `paid_body_hash` が含まれますが、DB の `draft` 行にはこれらを保存しません。

理由:
- `teaser_hash` は公開時に Pages JSON と照合するためのもの（W2-4）
- `paid_body_hash` も承認・公開フロー完了後に確定する値
- draft 段階で hash を DB に書き込んでしまうと、内容修正時に不整合が生じる

hash の DB への書き込みは公開処理（W2-4）で行います。

## dry-run

`--dry-run` では以下まで実施します:

- draft 読み込み
- JSON Schema 検証
- hash 再計算
- restricted leak check
- period 整合確認
- DB 保存用 payload 構築
- プレビュー表示

`--dry-run` では DB への INSERT / UPDATE / DELETE は行いません。
実行終了時に `dry-run: DBへの書き込みは行っていません。` と表示します。

## 冪等再実行

同一 week_id の draft を2回実行した場合:

- **完全一致の場合**: `既に同一 draft が保存されています。` として終了コード 0
- **内容が異なる場合**: `同じ week_id に異なる draft が存在します。` として停止

比較対象: `week_id`, `title`, `period_start`, `period_end`, `revision`, `generated_at`, `free_teaser`（JSON canonical）, `paid_body`（JSON canonical）

## 衝突時の停止

既存行の status に応じて以下の動作をします:

| 既存 status | 動作 |
|---|---|
| `draft`（同一内容） | 冪等成功（終了コード 0） |
| `draft`（内容異なる） | 停止（終了コード 1） |
| `published` | 停止「既に published です」（終了コード 1） |
| `withdrawn` | 停止「既に withdrawn です」（終了コード 1） |

`--replace` は実装していません。

## published / withdrawn 上書き禁止

`status='published'` または `status='withdrawn'` の行は、draft で上書きできません。

`withdrawn` 後の再発行は、revision 設計を含む公開後機能として別途設計します。

## 本番ガード

- `--production` なしで本番 URL（`lvsustmfqrxjnfgdtlna` を含む）への INSERT を拒否
- URL 文字列の部分一致ではなく、hostname で判定
- `--dry-run` では本番 URL でも DB 書き込みなし
- W2-3 完了時点では本番 `weekly_reports` へ保存しない

## warnings の扱い

ローカル draft の `warnings` は DB に専用カラムがありません。W2-3 では:

- warnings 件数と要旨を保存前に表示
- `free_teaser` / `paid_body` への追加は行わない
- `weekly_reports` に別フィールドは追加しない
- migration 変更はしない

warnings の永続化が必要な場合は、将来の運用ログ機能として別設計します。

## W2-4 との境界

| 項目 | W2-3 | W2-4 |
|---|---|---|
| `weekly_reports` への draft INSERT | ✓ | - |
| `teaser_hash` の DB 書き込み | - | ✓ |
| `paid_body_hash` の DB 書き込み | - | ✓ |
| `reviewed_at` / `reviewed_by` の設定 | - | ✓ |
| `status='published'` への更新 | - | ✓ |
| Pages JSON 生成 | - | ✓ |
| ローカル draft の archives 移動 | - | ✓ |
