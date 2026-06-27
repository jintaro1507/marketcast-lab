# Weekly Marketcast 公開オペレーション（W2-4B）

## 概要

承認済み Weekly Marketcast の公開フローを説明します。
GitHub Pages teaser 生成、デプロイ確認、DB published 化、draft アーカイブを段階的に行います。

## 公開フロー全体像

```
[draft 生成] → [DB保存] → [承認] → [公開準備] → [git push] → [Pages確認] → [DB published化] → [完了]
  W2-1         W2-3       W2-4A      prepare      operator    verify          finalize
```

## 前提条件

1. `generate_weekly_draft.py` で draft ファイルが生成済み
2. `save_weekly_report_draft.py` で DB に `status=draft` として保存済み
3. `approve_weekly_report.py` で承認ファイルが作成済み
4. `OPERATOR_NAME` 環境変数が設定済み

## ステップ 1: 公開準備（prepare）

承認済み draft から公開 JSON ファイルを生成します。

```bash
# 通常実行（DB 確認あり）
OPERATOR_NAME=your-name python scripts/prepare_weekly_publication.py --week-id 2026-W26

# draft ファイルを明示指定
OPERATOR_NAME=your-name python scripts/prepare_weekly_publication.py \
  --week-id 2026-W26 \
  --draft-path /path/to/draft.json \
  --approval-path /path/to/approval.json

# dry-run（書き込みなし）
OPERATOR_NAME=your-name python scripts/prepare_weekly_publication.py \
  --week-id 2026-W26 --dry-run
```

### 生成されるファイル

```
data/weekly/2026-W26.json    ← 公開ティーザー（git-tracked）
data/weekly/index.json       ← 週次インデックス（git-tracked）
~/.local/share/marketcast-lab/publications/2026-W26_publication.json  ← pub state
```

### 確認入力

```
PREPARE 2026-W26
```

## ステップ 2: git push（operator 手動）

prepare 完了後、operator が手動で git 操作を実施します。
**CLI は git コマンドを実行しません。**

```bash
git add data/weekly/2026-W26.json data/weekly/index.json
git commit -m "Publish Weekly Marketcast 2026-W26"
git push
```

GitHub Actions が実行され、GitHub Pages にデプロイされます。
デプロイ完了まで数分かかります。

## ステップ 3: Pages 確認（verify）

デプロイ後、Pages に正しく反映されたことを確認します。

```bash
python scripts/verify_weekly_pages.py --week-id 2026-W26
```

- 最大 5 回リトライ（15 秒間隔）
- teaser ファイルと index ファイルの両方を確認
- 成功後、pub state を `pages_verified` に更新

### 確認内容

| 項目 | 確認方法 |
|---|---|
| teaser_hash | 公開 JSON の teaser_hash が承認ファイルと一致 |
| week_id / revision | 公開 JSON のメタデータ一致 |
| free_teaser hash | 公開 JSON の free_teaser 内容を再計算して照合 |
| index エントリ | index.json に当該週のエントリが存在 |

## ステップ 4: DB published 化（finalize）

Pages 確認後、DB を `published` に遷移させます。

```bash
OPERATOR_NAME=your-name python scripts/finalize_weekly_publication.py --week-id 2026-W26

# dry-run（DB 更新・アーカイブなし）
OPERATOR_NAME=your-name python scripts/finalize_weekly_publication.py \
  --week-id 2026-W26 --dry-run
```

### 実行内容

1. pub state が `pages_verified` であることを確認
2. OPERATOR_NAME が承認ファイルの `approved_by` と一致することを確認
3. draft / 承認ファイルの再検証
4. DB の再確認（`verify_pre_publish`）
5. Pages の再検証（1 パス）
6. DB を `draft → published` に遷移（pub state の `published_at` を使用）
7. DB 整合確認（`verify_published_report`）
8. Pages / DB の `teaser_hash` 照合
9. draft をアーカイブ
10. pub state を `completed` に更新

### 確認入力

```
PUBLISH 2026-W26
```

## 公開ステートファイル

### 保存先

```
~/.local/share/marketcast-lab/publications/YYYY-WXX_publication.json
```

- ディレクトリ権限: `700`（オーナーのみ）
- ファイル権限: `600`（オーナーのみ）

### ステージ遷移

```
prepared → pages_verified → db_published → completed
```

| ステージ | 操作 |
|---|---|
| `prepared` | prepare_weekly_publication.py 実行後 |
| `pages_verified` | verify_weekly_pages.py 確認後 |
| `db_published` | finalize_weekly_publication.py DB 更新後 |
| `completed` | archive 完了後 |

### ファイル構造

```json
{
  "publication_version": 1,
  "week_id": "2026-W26",
  "revision": 1,
  "teaser_hash": "64文字の小文字 SHA-256",
  "paid_body_hash": "64文字の小文字 SHA-256",
  "published_at": "2026-06-29T01:30:00+00:00",
  "stage": "completed",
  "prepared_at": "2026-06-29T01:30:00+00:00",
  "pages_verified_at": "2026-06-29T01:45:00+00:00",
  "db_published_at": "2026-06-29T01:46:00+00:00",
  "completed_at": "2026-06-29T01:46:30+00:00"
}
```

## アーカイブファイル

### 保存先

```
~/.local/share/marketcast-lab/archives/YYYY-WXX_draft.json
```

- ディレクトリ権限: `700`（オーナーのみ）
- ファイル権限: `600`（オーナーのみ）
- 内容: 公開前のローカル draft ファイルのコピー

## 公開 JSON の構造

### teaser（data/weekly/YYYY-WXX.json）

```json
{
  "schema_version": 1,
  "week_id": "2026-W26",
  "revision": 1,
  "published_at": "2026-06-29T01:30:00+00:00",
  "teaser_hash": "64文字の小文字 SHA-256",
  "free_teaser": {
    "week_id": "2026-W26",
    "title": "Weekly Marketcast 2026年第26週（6/22〜6/26）",
    ...
  }
}
```

**公開 JSON に含まれないもの:**
- `paid_body`（有料本文）
- `paid_body_hash`
- `approved_by` / `reviewed_by`
- restricted 生値（price, close, value など）
- スコア・Timeline・反応詳細

### index（data/weekly/index.json）

```json
{
  "schema_version": 1,
  "updated_at": "2026-06-29T01:30:00+00:00",
  "latest_week_id": "2026-W26",
  "reports": [
    {
      "week_id": "2026-W26",
      "revision": 1,
      "published_at": "2026-06-29T01:30:00+00:00",
      "title": "Weekly Marketcast ...",
      "period_start": "2026-06-22",
      "period_end": "2026-06-26",
      "env_label": "...",
      "teaser_hash": "..."
    }
  ]
}
```

## セキュリティ制約

| 制約 | 詳細 |
|---|---|
| paid_body 非公開 | 公開 JSON に paid_body を含めない |
| restricted 生値非公開 | price, close, value などを含めない |
| シークレット非出力 | api_key, jwt などを含めない |
| git 非自動実行 | operator が手動で git push |
| 本番ガード | `--production` なしで本番 Supabase に接続しない |
| 承認者確認 | finalize 時に OPERATOR_NAME == approved_by を確認 |

## Pages 検証の仕組み

- URL: `https://marketcast.oneshorejp.com/data/weekly/YYYY-WXX.json?v=<hash[:12]>`
- キャッシュバスト: `?v=<teaser_hash[:12]>`
- リトライ: 最大 5 回 / 15 秒間隔
- 確認項目: teaser_hash, week_id, revision, published_at, free_teaser hash

## 本番 Supabase ガード

| 条件 | 動作 |
|---|---|
| ローカル Supabase | 通常動作 |
| 本番 Supabase + `--production` なし | エラー停止 |
| 本番 Supabase + `--production` あり | DB 接続・published 更新を許可 |

## 冪等性

| ステップ | 冪等動作 |
|---|---|
| prepare（同一内容） | pub state 存在確認 → 終了コード 0 |
| teaser ファイル（同一内容） | 上書きなし、"unchanged" |
| index エントリ（同一 week_id・hash） | 変更なし |
| verify（pages_verified 済み） | 冪等終了コード 0 |
| finalize（completed 済み） | 冪等終了コード 0 |
