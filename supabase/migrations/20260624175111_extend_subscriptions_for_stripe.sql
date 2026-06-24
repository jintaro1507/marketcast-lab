-- Extend subscriptions for Stripe synchronization.
--
-- price_id:
--   Stripe Price ID associated with the current subscription.
--
-- cancel_at_period_end:
--   Whether Stripe is scheduled to cancel the subscription at the end
--   of the current billing period.
--
-- updated_at:
--   Automatically refreshed whenever the row is updated.

alter table public.subscriptions
  add column price_id text,
  add column cancel_at_period_end boolean not null default false;

comment on column public.subscriptions.price_id is
  '現在の契約に紐づくStripe Price ID。Webhookから同期する。';

comment on column public.subscriptions.cancel_at_period_end is
  '現在の請求期間終了時に解約予定かを示す。Stripeから同期する。';

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists set_subscriptions_updated_at
  on public.subscriptions;

create trigger set_subscriptions_updated_at
before update on public.subscriptions
for each row
execute function public.set_updated_at();
