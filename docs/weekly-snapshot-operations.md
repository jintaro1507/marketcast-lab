# Weekly Marketcast — スナップショット運用メモ

## 必要な秘密情報

保存先: `~/.config/marketcast-lab/.env`

```
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
FRED_API_KEY=your_key_here
```

実値をこのファイルや Git に書かないこと。

## 権限設定

```bash
mkdir -m 700 ~/.config/marketcast-lab
touch ~/.config/marketcast-lab/.env
chmod 600 ~/.config/marketcast-lab/.env
# エディタで編集
nano ~/.config/marketcast-lab/.env
```

## 実行フロー

### 1. dry-run（確認のみ）

```bash
python scripts/save_weekly_snapshot.py --week-id 2026-W26 --dry-run
```

DB 書き込みは行わない。プレビューと HARD/WARN 判定のみ表示する。

### 2. 通常スナップ保存（ローカル Supabase）

```bash
python scripts/save_weekly_snapshot.py --week-id 2026-W26
```

`SUPABASE_URL` がローカル URL の場合のみ書き込み可能。確認プロンプトあり。

### 3. 通常スナップ保存（本番 Supabase）

```bash
python scripts/save_weekly_snapshot.py --week-id 2026-W26 --production
```

`--production` フラグが必要。本番への書き込みが有効になる。

### 4. 初号 seed 投入

```bash
python scripts/seed_initial_snapshot.py --week-id 2026-W25 --dry-run
python scripts/seed_initial_snapshot.py --week-id 2026-W25 --production
```

- `seeded=True` / `seed_source="initial_seed_from_source_series"` で保存される。
- 同一週に通常スナップまたは seed スナップが既に存在する場合は停止する。
- 上書き（--replace）は初期版では未実装。

### 5. 週次差分計算

```bash
python scripts/calculate_weekly_changes.py --week-id 2026-W26
python scripts/calculate_weekly_changes.py --week-id 2026-W26 --out changes.json
```

- 当週スナップと前週スナップを DB から取得して差分を計算する。
- 出力 JSON に `current_value` / `previous_value` は含まれない。
- `hard_errors` が空の場合のみ exit 0。

## HARD / WARN の扱い

### HARD（DB 書き込みを中止する）

| 条件 | 説明 |
|------|------|
| week_id 不正 | フォーマット違反または存在しない ISO 週 |
| status!=ok が 2 件以上 | 複数資産の取得失敗 |
| as_of が対象週外が 2 件以上 | データの週ずれ |
| restricted 規則不一致 | gold/sp500 が false など |
| 前週値 0 で変化率計算不可 | ゼロ除算 |
| DB 件数が 6 件でない | 書き込み不整合 |

### WARN（書き込みは続行するが注意が必要）

| 条件 | 説明 |
|------|------|
| status!=ok が 1 件 | 1 資産の取得失敗 |
| as_of が period_end より古い | 週内だが最終営業日より前 |
| Yahoo フォールバック使用 | Stooq からの取得失敗 |
| seeded スナップ | 前週値が seed データ |

## 本番書き込み防止

- `--production` フラグなしでは本番 URL への書き込みを拒否する。
- テストは `SUPABASE_TEST_URL` 環境変数でローカル URL を指定する。
- 本番 URL（project ref: `lvsustmfqrxjnfgdtlna`）がテストで検出された場合はスキップする。

## restricted 生値の非表示

- `gold` / `sp500` の生値（`value` フィールド）は DB 内にのみ保存する。
- プレビュー出力では全資産の `value` を表示しない。
- 差分計算出力（`calculate_weekly_changes.py`）には `current_value` / `previous_value` を含めない。
- `pct_change` / `pt_change` / `direction` のみを公開する。

## 6 資産定義

| asset_key | 取得元 | restricted |
|-----------|--------|-----------|
| wti | FRED DCOILWTICO | false |
| gold | Stooq gld.us → Yahoo GLD | **true** |
| sp500 | FRED SP500 | **true** |
| ust10y | FRED DGS10 | false |
| usdjpy | FRED DEXJPUS | false |
| vix | FRED VIXCLS | false |

## トラブル時の停止条件

以下の場合は即座に中止し、手動確認を行うこと。

- HARD エラーが発生した場合
- restricted 規則の不一致が検出された場合
- DB 書き込み件数が 6 件でない場合
- 差分出力に `current_value` / `previous_value` が含まれた場合（バグ）

## ローカル統合テスト実行

```bash
# ローカル Supabase を起動
supabase start

# 統合テスト実行
python -m unittest tests/test_weekly_db_local.py -v

# 単体テスト実行（ネットワーク不要）
python -m unittest tests/test_weekly_dates.py tests/test_weekly_snapshot.py tests/test_weekly_changes.py -v
```
