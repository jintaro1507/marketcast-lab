-- stripe_webhook_events
-- Stripe Webhook の冪等性管理テーブル。
-- 処理成功後にイベント ID を記録する「記録at終了」方式を採用。
--   - 処理前: event_id が存在しなければ処理を実行する
--   - 処理後: 成功時のみ INSERT する → 失敗時は記録されず Stripe が再送できる
--   - 並列受信時: 両方が処理を開始しうるが、subscriptions の upsert は冪等なため
--     最終結果は正しくなる。イベント ID の INSERT は ON CONFLICT DO NOTHING で重複を無視。
-- service_role 以外のアクセスを RLS で禁止する。

CREATE TABLE IF NOT EXISTS public.stripe_webhook_events (
  event_id    text        PRIMARY KEY,            -- Stripe evt_... ID
  event_type  text        NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.stripe_webhook_events ENABLE ROW LEVEL SECURITY;

-- ポリシーなし = anon / authenticated はアクセス不可、service_role は RLS をバイパスして全操作可能
-- 念のため明示的に anon / authenticated の SELECT を禁止するポリシーを追加する
CREATE POLICY "deny_all_non_service_role"
  ON public.stripe_webhook_events
  FOR ALL
  TO anon, authenticated
  USING (false)
  WITH CHECK (false);
