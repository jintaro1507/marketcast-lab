-- Marketcast Lab subscriptions baseline
--
-- The production table already existed before Supabase migration history
-- was introduced. This migration reproduces that schema for fresh
-- environments. The linked production database has this version recorded
-- as applied and must not execute this file again.

create table public.subscriptions (
  user_id uuid not null,
  stripe_customer_id text,
  stripe_subscription_id text,
  status text not null,
  current_period_end timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint subscriptions_pkey
    primary key (user_id),

  constraint subscriptions_stripe_customer_id_key
    unique (stripe_customer_id),

  constraint subscriptions_stripe_subscription_id_key
    unique (stripe_subscription_id),

  constraint subscriptions_status_check
    check (
      status = any (
        array[
          'incomplete'::text,
          'incomplete_expired'::text,
          'trialing'::text,
          'active'::text,
          'past_due'::text,
          'canceled'::text,
          'unpaid'::text,
          'paused'::text
        ]
      )
    ),

  constraint subscriptions_user_id_fkey
    foreign key (user_id)
    references auth.users(id)
    on delete cascade
);

comment on table public.subscriptions is
  'Marketcast Labの契約状態。1ユーザー1行。行なし＝free。statusにfreeは保存しない。';

comment on column public.subscriptions.user_id is
  'auth.users.idと1対1で紐付く主キー。ON DELETE CASCADEでユーザー削除時に自動削除。';

comment on column public.subscriptions.status is
  'Stripeのサブスクリプションステータス。freeは保存しない。CHECK制約で8値に限定。';

comment on column public.subscriptions.current_period_end is
  '現在の請求期間終了日時（UTC）。B1時点ではNULL許容。Webhookが書き込む。';

alter table public.subscriptions enable row level security;

create policy subscriptions_select_own
  on public.subscriptions
  for select
  to authenticated
  using (
    auth.uid() is not null
    and auth.uid() = user_id
  );

grant select
  on table public.subscriptions
  to authenticated;

grant all
  on table public.subscriptions
  to service_role;
