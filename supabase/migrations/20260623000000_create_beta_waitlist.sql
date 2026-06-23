-- Migration: create beta_waitlist
-- Created: 2026-06-23
-- Apply via: Supabase Dashboard > SQL Editor, または supabase db push
--
-- 冪等性について:
--   テーブル・制約・インデックスは CREATE IF NOT EXISTS のため再実行可能。
--   RLS ポリシーは DROP IF EXISTS → CREATE の順で記述しており再実行可能。
--   ただし既存のポリシー設定を上書きするため、本番環境での再実行前に
--   影響を確認してください。

-- ─── テーブル作成 ───────────────────────────────────────────────────
create table if not exists public.beta_waitlist (
  id         uuid        primary key default gen_random_uuid(),
  email      text        not null,
  purpose    text        null,
  consent    boolean     not null,
  source     text        null,
  created_at timestamptz not null default now(),

  -- email 重複防止
  constraint beta_waitlist_email_unique unique (email),

  -- purpose は決められた値のみ許可
  constraint beta_waitlist_purpose_check check (
    purpose is null or purpose in (
      'investment',
      'learning',
      'work_research',
      'education',
      'other'
    )
  ),

  -- consent=true のみ許可
  constraint beta_waitlist_consent_true check (consent = true),

  -- source 長さ制限（任意文字列の無制限保存を防止）
  constraint beta_waitlist_source_length check (
    source is null or char_length(source) <= 32
  )
);

comment on table public.beta_waitlist is 'β版事前登録者のウェイトリスト';
comment on column public.beta_waitlist.email is '登録者のメールアドレス（一意）';
comment on column public.beta_waitlist.purpose is '利用目的（任意）';
comment on column public.beta_waitlist.consent is 'プライバシーポリシー同意フラグ（常にtrue）';
comment on column public.beta_waitlist.source is '流入元（URLパラメータ source の値）';

-- ─── RLS 設定 ────────────────────────────────────────────────────────
-- RLS を有効化
alter table public.beta_waitlist enable row level security;

-- 匿名ユーザーは INSERT のみ許可（自分のレコードかどうかを問わない INSERT専用）
-- SELECT / UPDATE / DELETE は許可しない
-- 既存ポリシーがある場合は削除してから再作成する（冪等化）
drop policy if exists "anon_insert_only" on public.beta_waitlist;

create policy "anon_insert_only"
  on public.beta_waitlist
  for insert
  to anon
  with check (consent = true);

-- 認証済みユーザー（サービス管理者）は全操作を許可
-- 必要に応じてsupabase_adminロールまたはservice_roleを利用してください
-- create policy "authenticated_all"
--   on public.beta_waitlist
--   for all
--   to authenticated
--   using (true)
--   with check (true);

-- ─── インデックス ─────────────────────────────────────────────────────
-- email は UNIQUE 制約があるため自動でインデックスが作成される
-- 追加で created_at 降順インデックスを作成（管理時の参照用）
create index if not exists beta_waitlist_created_at_idx
  on public.beta_waitlist (created_at desc);
