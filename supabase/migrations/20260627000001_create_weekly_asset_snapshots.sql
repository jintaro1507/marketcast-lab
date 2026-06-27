-- Weekly Marketcast: 週次資産スナップショットテーブル
--
-- 週末基準値（各ISO週の最後の有効観測値）を保存する内部データテーブル。
-- restricted=true の資産（gold, sp500）の生値を含む可能性があるため、
-- RLS を有効化し、ポリシーを一切作成しない（default deny）。
-- service_role は RLS をバイパスするため、スクリプトからのみアクセスする。
--
-- public teaser・paid body・Pages JSON への生値の複製は禁止。
-- 週次変化率（派生値）は非restricted資産のみ公開可能。
--
-- 公開処理フロー（参考: W2以降で実装）:
--   1. save_weekly_snapshot.py → 本テーブルへ upsert（service role）
--   2. generate_weekly.py → 本テーブルから読み込み、週次変化を計算
--   3. approve_weekly.py → weekly_reports へ保存（restricted生値は含めない）
--   4. Pages JSONを生成 → git commit → 手動push
--   5. DB status を published へ更新
-- 途中失敗時は各段階から再実行可能な設計とする。

CREATE TABLE public.weekly_asset_snapshots (
  week_id           TEXT         NOT NULL,
  asset_key         TEXT         NOT NULL,
  source            TEXT         NOT NULL,
  value             NUMERIC,
  as_of             DATE,
  status            TEXT         NOT NULL DEFAULT 'ok',
  restricted        BOOLEAN      NOT NULL,
  seeded            BOOLEAN      NOT NULL DEFAULT FALSE,
  seed_source       TEXT,
  snapshot_taken_at TIMESTAMPTZ  NOT NULL,
  created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  PRIMARY KEY (week_id, asset_key),

  -- week_id: フォーマット検証のみ（01〜53）。
  -- 実在するISO週かどうかは datetime.date.fromisocalendar() でアプリ側が検証する。
  CONSTRAINT snapshots_week_id_format CHECK (
    week_id ~ '^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$'
  ),

  -- asset_key: 対象6資産のみ許可
  CONSTRAINT snapshots_asset_key_values CHECK (
    asset_key IN ('wti', 'gold', 'sp500', 'ust10y', 'usdjpy', 'vix')
  ),

  -- status: 許容値
  CONSTRAINT snapshots_status_values CHECK (
    status IN ('ok', 'no_data', 'error')
  ),

  -- restricted は asset_key で固定。アプリ側指定に依存しない。
  -- gold, sp500: 生値再配布制限あり → restricted = TRUE 強制
  -- wti, ust10y, usdjpy, vix: 制限なし → restricted = FALSE 強制
  CONSTRAINT snapshots_restricted_by_asset CHECK (
    (asset_key IN ('gold', 'sp500') AND restricted = TRUE)
    OR
    (asset_key IN ('wti', 'ust10y', 'usdjpy', 'vix') AND restricted = FALSE)
  ),

  -- status と value/as_of の整合
  -- status='ok': 取得成功 → value, as_of は両方 NOT NULL
  -- status='no_data'/'error': 取得失敗 → value, as_of は両方 NULL
  -- as_of に NULL を使う。対象週は week_id から復元可能。
  CONSTRAINT snapshots_value_consistency CHECK (
    (status = 'ok'
      AND value IS NOT NULL
      AND as_of IS NOT NULL)
    OR
    (status IN ('no_data', 'error')
      AND value IS NULL
      AND as_of IS NULL)
  ),

  -- seed: seeded=true のときのみ seed_source を必須とする
  CONSTRAINT snapshots_seed_consistency CHECK (
    (seeded = FALSE AND seed_source IS NULL)
    OR
    (seeded = TRUE  AND seed_source IS NOT NULL)
  )
);

-- RLS 有効化（ポリシー未作成 = anon/authenticated からの全操作を default deny）
-- service_role は RLS をバイパスするため追加設定不要
ALTER TABLE public.weekly_asset_snapshots ENABLE ROW LEVEL SECURITY;

-- updated_at 自動更新
-- public.set_updated_at() は 20260624175111_extend_subscriptions_for_stripe.sql で定義済み。
-- 新しい関数は作成しない。
CREATE TRIGGER set_weekly_asset_snapshots_updated_at
  BEFORE UPDATE ON public.weekly_asset_snapshots
  FOR EACH ROW
  EXECUTE FUNCTION public.set_updated_at();

COMMENT ON TABLE public.weekly_asset_snapshots IS
  '週次資産スナップショット。各ISO週の最後の有効観測値を保存する内部テーブル。'
  'restricted=true（gold, sp500）の生値を含む。service role のみアクセス可能。';

COMMENT ON COLUMN public.weekly_asset_snapshots.week_id IS
  'ISO 8601週番号（例: 2026-W25）。period_start（月曜）と period_end（金曜）はアプリ側で導出する。';
COMMENT ON COLUMN public.weekly_asset_snapshots.asset_key IS
  '資産識別子。wti/gold/sp500/ust10y/usdjpy/vix の6種類のみ。';
COMMENT ON COLUMN public.weekly_asset_snapshots.source IS
  'データ取得元（例: fred_DCOILWTICO, stooq_gld.us, fred_SP500）。';
COMMENT ON COLUMN public.weekly_asset_snapshots.value IS
  '週末基準値の生値。status=ok のみ有効。restricted=true の資産の値はこのテーブルにのみ保存し、公開出力へは複製しない。';
COMMENT ON COLUMN public.weekly_asset_snapshots.as_of IS
  '実際に取得した観測値の日付。対象週内の最後の有効営業日（土日は使わない）。status != ok のとき NULL。';
COMMENT ON COLUMN public.weekly_asset_snapshots.restricted IS
  '生値の再配布制限。asset_key によって固定（CHECK制約で保証）。gold/sp500=true, 他=false。';
COMMENT ON COLUMN public.weekly_asset_snapshots.seeded IS
  'true のとき、初期seed投入データ（正規のスナップショット取得フロー外で取得）。seed_source に取得根拠を記録する。';
COMMENT ON COLUMN public.weekly_asset_snapshots.snapshot_taken_at IS
  'save_weekly_snapshot.py の実行時刻（UTC）。鮮度判定には as_of を使用し、本列は監査目的。';

-- service_role に明示的な DML 権限を付与する。
-- Supabase では CREATE TABLE 後に service_role への grant が自動付与されない。
-- anon / authenticated への DML 権限は付与しない（RLS の default deny で保護）。
-- 既存パターン参考: 20260625131444_grant_stripe_tables_to_service_role.sql
GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLE public.weekly_asset_snapshots
  TO service_role;
