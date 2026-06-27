# Weekly Marketcast 承認オペレーション（W2-4A）

## 概要

Weekly Marketcast の公開フローにおける「承認」ステップを説明します。  
承認は **ローカルファイルへの記録** であり、DB は変更しません。

## 承認フローの全体像

```
[draft 生成] → [DB保存] → [承認] → [Pages反映確認(*)] → [published遷移(*)]
  W2-1         W2-3       W2-4A         W2-4B以降          W2-4B以降
```

`(*)` は W2-4A のスコープ外です。

## 前提条件

1. `generate_weekly_draft.py` で draft ファイルが生成済み
2. `save_weekly_report_draft.py` で DB に `status=draft` として保存済み
3. `OPERATOR_NAME` 環境変数が設定済み

## 承認コマンド

```bash
# 通常実行（DB 確認あり・承認ファイル保存）
OPERATOR_NAME=your-name python scripts/approve_weekly_report.py --week-id 2026-W26

# draft ファイルを明示指定
OPERATOR_NAME=your-name python scripts/approve_weekly_report.py \
  --week-id 2026-W26 \
  --draft-path /path/to/draft.json

# dry-run（DB 確認まで実施・承認ファイル書き込みなし）
OPERATOR_NAME=your-name python scripts/approve_weekly_report.py \
  --week-id 2026-W26 --dry-run
```

## 承認ファイル

### 保存先

```
~/.local/share/marketcast-lab/approvals/YYYY-WXX_approval.json
```

- ディレクトリ権限: `700`（オーナーのみ）
- ファイル権限: `600`（オーナーのみ）

### ファイル構造

```json
{
  "week_id": "2026-W26",
  "revision": 1,
  "approved_at": "2026-06-29T01:00:00+00:00",
  "approved_by": "your-name",
  "draft_generated_at": "2026-06-28T01:00:00+00:00",
  "teaser_hash": "64文字の小文字 SHA-256",
  "paid_body_hash": "64文字の小文字 SHA-256",
  "approval_version": 1
}
```

**承認ファイルに含まれないもの:**
- `free_teaser`（本文）
- `paid_body`（本文）
- シークレット・機密情報

### ハッシュの役割

| フィールド | 対象 | 用途 |
|---|---|---|
| `teaser_hash` | `free_teaser` の SHA-256 | Pages 反映後の改変検出 |
| `paid_body_hash` | `paid_body` の SHA-256 | 公開時の整合確認 |

## OPERATOR_NAME 環境変数

承認者識別子は **環境変数のみ** から取得します（CLI フラグ不可）。

```bash
export OPERATOR_NAME=your-name
```

- 必須: 設定なしはエラー
- 長さ: 1〜64 文字
- デフォルト値なし

## 承認確認入力

承認時は以下の形式で **完全一致** する文字列を入力します:

```
APPROVE 2026-W26
```

- `yes` / `y` / `ok` などは受け付けません
- 別の week_id を入力してもキャンセルになります

## 冪等性

同一の承認ファイルが既に存在する場合:
- **内容が同一** → 冪等成功（ファイル変更なし、終了コード 0）
- **内容が異なる** → エラー停止（終了コード 1）

## DB 変更なし

承認ステップでは `weekly_reports` の `status` は `draft` のままです。  
DB が `published` に遷移するのは **Pages 反映確認後** です（W2-4B 以降）。

| カラム | 承認後 |
|---|---|
| `status` | `draft`（変更なし） |
| `reviewed_at` | NULL（変更なし） |
| `reviewed_by` | NULL（変更なし） |
| `teaser_hash` | NULL（変更なし） |
| `paid_body_hash` | NULL（変更なし） |

## published 遷移（W2-4B 以降）

published 遷移は以下の内部関数で実装されています（ローカル DB テスト専用）:

```python
from weekly_report_publish import apply_published_transition

db_row = apply_published_transition(db, week_id, approval)
```

### PATCH 条件

```
PATCH /weekly_reports
  ?week_id=eq.{week_id}
  &status=eq.draft
  &revision=eq.{revision}
Prefer: return=representation
```

期待更新行数: **1 件**（0 件・複数件はエラー）

### published PATCH payload

```json
{
  "status": "published",
  "reviewed_at": "<承認ファイルの approved_at>",
  "reviewed_by": "<承認ファイルの approved_by>",
  "published_at": "<Pages反映確認後の現在時刻>",
  "teaser_hash": "<承認済み teaser_hash>",
  "paid_body_hash": "<承認済み paid_body_hash>",
  "withdrawn_at": null,
  "withdrawal_reason": null
}
```

**payload に含まれないもの:**
`week_id`, `title`, `period_start`, `period_end`, `free_teaser`, `paid_body`, `revision`, `generated_at`

## 本番ガード

| 条件 | 動作 |
|---|---|
| ローカル Supabase | 通常動作 |
| 本番 Supabase + `--production` なし | エラー停止 |
| 本番 Supabase + `--production` あり | DB 接続（ただし承認は DB を変更しない） |

本番 DB の `status=published` への遷移は **W2-4A スコープ外** です。

## セキュリティ制約

- 本番 Supabase へアクセスしない（承認ステップ）
- 本番 DB を published 化しない
- Pages JSON を生成しない
- git push を行わない
- restricted 生値を出力しない
- secret を出力しない
- 承認ファイルに本文（free_teaser / paid_body）を含めない
