-- Weekly Marketcast: 週次レポートテーブル
--
-- 生成・承認・公開された週次レポートを保存する。
-- free_teaser（無料公開ティーザー）と paid_body（有料本文）を JSONB で保持する。
-- restricted生値（gold/sp500の生値）は一切含まない。
-- RLS を有効化し、ポリシーを一切作成しない（default deny）。
--
-- コンテンツ配信経路:
--   free_teaser: Pages 上の静的 JSON（data/weekly/YYYY-WXX.json）から取得
--   paid_body:   get-weekly-marketcast Edge Function 経由でのみ返す
--               （JWT 検証 → subscription 判定 → service role で SELECT）
--
-- revision について:
--   同一 week_id の修正回数カウンタ。初回=1、修正再公開で+1。
--   過去 revision の本文履歴は保持しない（正規版初期スコープ外）。
--   PK は week_id のみ。revision 2+ での過去履歴保存は公開後機能へ回す。
--
-- 公開処理フロー（W2以降で実装）:
--   1. draft生成 → status='draft' で INSERT（teaser/paid_body_hash は NULL）
--   2. 運営者承認
--   3. public teaserファイル生成（data/weekly/YYYY-WXX.json）
--   4. git commit → 手動 push → Pages 反映確認
--   5. DB を status='published' へ UPDATE（hash を付与）
--   6. 有料API確認
--   7. teaser_hash を DB と Pages JSON で照合
-- 途中失敗時は各段階から再実行可能な設計とする。
-- Supabase の published 化と git push を単一トランザクションのように扱わない。

CREATE TABLE public.weekly_reports (
  week_id           TEXT         NOT NULL PRIMARY KEY,
  title             TEXT         NOT NULL,
  period_start      DATE         NOT NULL,
  period_end        DATE         NOT NULL,
  status            TEXT         NOT NULL DEFAULT 'draft',
  free_teaser       JSONB        NOT NULL,
  paid_body         JSONB        NOT NULL,
  teaser_hash       TEXT,
  paid_body_hash    TEXT,

  -- revision: 修正再公開時にインクリメント。初回=1。
  -- 過去 revision の本文履歴保存は正規版初期スコープ外。
  revision          INTEGER      NOT NULL DEFAULT 1,

  generated_at      TIMESTAMPTZ  NOT NULL,
  reviewed_at       TIMESTAMPTZ,
  reviewed_by       TEXT,
  published_at      TIMESTAMPTZ,
  withdrawn_at      TIMESTAMPTZ,
  withdrawal_reason TEXT,
  created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  -- week_id: フォーマット検証のみ。実在確認はアプリ側で行う。
  CONSTRAINT reports_week_id_format CHECK (
    week_id ~ '^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$'
  ),

  -- status: 許容値
  CONSTRAINT reports_status_values CHECK (
    status IN ('draft', 'published', 'withdrawn')
  ),

  -- period: period_end は period_start 以降
  CONSTRAINT reports_period_order CHECK (
    period_end >= period_start
  ),

  -- revision: 1 以上
  CONSTRAINT reports_revision_positive CHECK (
    revision >= 1
  ),

  -- reviewed_by: 1〜64文字の非空文字列（published/withdrawn 時は必須）
  CONSTRAINT reports_reviewed_by_length CHECK (
    reviewed_by IS NULL
    OR (length(reviewed_by) >= 1 AND length(reviewed_by) <= 64)
  ),

  -- withdrawal_reason: 空文字不可、最大500文字
  CONSTRAINT reports_withdrawal_reason_length CHECK (
    withdrawal_reason IS NULL
    OR (length(withdrawal_reason) >= 1 AND length(withdrawal_reason) <= 500)
  ),

  -- teaser_hash: SHA-256 16進小文字 64文字
  CONSTRAINT reports_teaser_hash_format CHECK (
    teaser_hash IS NULL OR teaser_hash ~ '^[0-9a-f]{64}$'
  ),

  -- paid_body_hash: SHA-256 16進小文字 64文字
  CONSTRAINT reports_paid_body_hash_format CHECK (
    paid_body_hash IS NULL OR paid_body_hash ~ '^[0-9a-f]{64}$'
  ),

  -- status='draft' の状態制約（全ての審査・公開情報が未設定）
  CONSTRAINT reports_draft_state CHECK (
    status != 'draft' OR (
      reviewed_at       IS NULL AND
      reviewed_by       IS NULL AND
      published_at      IS NULL AND
      withdrawn_at      IS NULL AND
      withdrawal_reason IS NULL AND
      teaser_hash       IS NULL AND
      paid_body_hash    IS NULL
    )
  ),

  -- status='published' の状態制約
  CONSTRAINT reports_published_state CHECK (
    status != 'published' OR (
      reviewed_at       IS NOT NULL AND
      reviewed_by       IS NOT NULL AND
      published_at      IS NOT NULL AND
      withdrawn_at      IS NULL     AND
      withdrawal_reason IS NULL     AND
      teaser_hash       IS NOT NULL AND
      paid_body_hash    IS NOT NULL
    )
  ),

  -- status='withdrawn' の状態制約
  CONSTRAINT reports_withdrawn_state CHECK (
    status != 'withdrawn' OR (
      reviewed_at       IS NOT NULL AND
      reviewed_by       IS NOT NULL AND
      published_at      IS NOT NULL AND
      withdrawn_at      IS NOT NULL AND
      withdrawal_reason IS NOT NULL AND
      teaser_hash       IS NOT NULL AND
      paid_body_hash    IS NOT NULL
    )
  )
);

-- RLS 有効化（ポリシー未作成 = anon/authenticated からの全操作を default deny）
-- service_role は RLS をバイパスするため追加設定不要
ALTER TABLE public.weekly_reports ENABLE ROW LEVEL SECURITY;

-- updated_at 自動更新（既存 public.set_updated_at() を再利用）
CREATE TRIGGER set_weekly_reports_updated_at
  BEFORE UPDATE ON public.weekly_reports
  FOR EACH ROW
  EXECUTE FUNCTION public.set_updated_at();

COMMENT ON TABLE public.weekly_reports IS
  '週次レポート。free_teaser と paid_body を保持する。restricted生値は含まない。service role のみアクセス可能。';

COMMENT ON COLUMN public.weekly_reports.week_id IS
  'ISO 8601週番号（例: 2026-W25）。PRIMARY KEY。';
COMMENT ON COLUMN public.weekly_reports.period_start IS
  '対象週の月曜日（ISO週の開始日）。';
COMMENT ON COLUMN public.weekly_reports.period_end IS
  '対象週の金曜日（市場最終営業日の基準日）。実際の最終取引日（as_of）とは異なる場合がある。';
COMMENT ON COLUMN public.weekly_reports.free_teaser IS
  '無料公開ティーザー（JSONB）。weekly_free_teaser.schema.json に準拠。restricted生値・有料情報を含まない。';
COMMENT ON COLUMN public.weekly_reports.paid_body IS
  '有料本文（JSONB）。weekly_paid_body.schema.json に準拠。restricted生値を含まない（end_value=null for gold/sp500）。';
COMMENT ON COLUMN public.weekly_reports.teaser_hash IS
  'free_teaser のカノニカル SHA-256（小文字16進64文字）。Pages JSON にも同値を掲載し、公開後に照合する。draft 時は NULL。';
COMMENT ON COLUMN public.weekly_reports.paid_body_hash IS
  'paid_body のカノニカル SHA-256（小文字16進64文字）。Supabase 内にのみ保存。draft 時は NULL。';
COMMENT ON COLUMN public.weekly_reports.revision IS
  '同一 week_id の修正回数。初回=1、修正再公開で +1。過去 revision の本文履歴は保持しない。';
COMMENT ON COLUMN public.weekly_reports.reviewed_by IS
  '承認した運営者の識別子（OPERATOR_NAME 環境変数から設定）。1〜64文字。published/withdrawn 時は必須。';
COMMENT ON COLUMN public.weekly_reports.withdrawal_reason IS
  '保留理由（1〜500文字、空文字不可）。withdrawn 時のみ必須。';

-- service_role に明示的な DML 権限を付与する。
-- anon / authenticated への DML 権限は付与しない（RLS の default deny で保護）。
-- 既存パターン参考: 20260625131444_grant_stripe_tables_to_service_role.sql
GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLE public.weekly_reports
  TO service_role;
