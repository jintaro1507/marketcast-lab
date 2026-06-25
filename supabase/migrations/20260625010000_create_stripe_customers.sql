-- stripe_customers: Stripe Customer ID ↔ Supabase user_id の永続対応テーブル
--
-- 役割分担:
--   stripe_customers … Stripe Customer と Supabase ユーザーの恒久的な 1:1 対応
--   subscriptions    … 現在・過去の契約状態（「行なし = free」の原則を維持）
--
-- Checkout / Webhook のいずれかで Customer が確定した時点で書き込まれ、
-- 以降の Checkout リクエストはこのテーブルを一次ソースとする。
-- Customer が存在しても subscriptions 行がなければ free ユーザーとして扱う。

CREATE TABLE IF NOT EXISTS public.stripe_customers (
  user_id            uuid        PRIMARY KEY
                                   REFERENCES auth.users(id) ON DELETE CASCADE,
  stripe_customer_id text        NOT NULL UNIQUE,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.stripe_customers IS
  'Stripe Customer ID と Supabase user_id の恒久的な 1:1 対応。'
  'Checkout・Webhook から書き込まれる。このテーブルは Customer の存在を示すのみで、'
  '契約状態は subscriptions テーブルが管理する。';

COMMENT ON COLUMN public.stripe_customers.user_id IS
  'Supabase auth.users.id（主キー）。ユーザー削除時はカスケード削除。';

COMMENT ON COLUMN public.stripe_customers.stripe_customer_id IS
  'Stripe Customer ID（cus_...）。1 ユーザー = 1 Customer の制約を UNIQUE で担保する。';

COMMENT ON COLUMN public.stripe_customers.created_at IS
  'レコード作成日時。';

COMMENT ON COLUMN public.stripe_customers.updated_at IS
  'レコード更新日時。set_updated_at トリガーにより自動更新。';

-- RLS: authenticated / anon からの直接アクセスを全拒否。service_role のみ操作する。
ALTER TABLE public.stripe_customers ENABLE ROW LEVEL SECURITY;

CREATE POLICY "deny_all_non_service_role"
  ON public.stripe_customers FOR ALL
  TO anon, authenticated
  USING (false)
  WITH CHECK (false);

-- updated_at 自動更新トリガー（public.set_updated_at は既存関数を再利用）
DROP TRIGGER IF EXISTS set_stripe_customers_updated_at
  ON public.stripe_customers;

CREATE TRIGGER set_stripe_customers_updated_at
  BEFORE UPDATE ON public.stripe_customers
  FOR EACH ROW
  EXECUTE FUNCTION public.set_updated_at();
