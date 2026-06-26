-- stripe_webhook_events: outcome追跡カラムの追加
--
-- 目的:
--   Webhookイベントの処理結果（applied / ignored / unresolved_user / duplicate_subscription）
--   およびStripe識別子を記録し、運用上の監査性と異常検知を向上させる。
--
-- 設計:
--   outcome    … 処理結果分類。DEFAULT 'applied' により既存行への影響なし。
--   error_code … 将来拡張用。現在は原則 NULL。
--   stripe_customer_id    … 取得できた場合のみ保存。個人情報は含まない。
--   stripe_subscription_id … 取得できた場合のみ保存。
--
-- RLS:
--   既存の "deny_all_non_service_role" ポリシーはすべてのカラムに適用される。
--   新カラムに対して anon / authenticated への追加権限は不要。
--
-- GRANT:
--   service_role は 20260625131444 で stripe_webhook_events への
--   SELECT, INSERT, UPDATE, DELETE が付与済み。
--   新カラムへのアクセスはテーブル単位権限に含まれるため追加GRANTは不要。

ALTER TABLE public.stripe_webhook_events
  ADD COLUMN outcome text NOT NULL DEFAULT 'applied'
    CONSTRAINT stripe_webhook_events_outcome_check
    CHECK (outcome IN ('applied', 'ignored', 'unresolved_user', 'duplicate_subscription')),
  ADD COLUMN error_code text,
  ADD COLUMN stripe_customer_id text,
  ADD COLUMN stripe_subscription_id text;

COMMENT ON COLUMN public.stripe_webhook_events.outcome IS
  '処理結果分類。'
  'applied=正常反映, ignored=対象外, '
  'unresolved_user=user_id解決不能, duplicate_subscription=重複Subscription検出';

COMMENT ON COLUMN public.stripe_webhook_events.error_code IS
  '将来拡張用。現在は原則NULL。';

COMMENT ON COLUMN public.stripe_webhook_events.stripe_customer_id IS
  '取得できたStripe Customer ID。payload全文は保存しない。';

COMMENT ON COLUMN public.stripe_webhook_events.stripe_subscription_id IS
  '取得できたStripe Subscription ID。payload全文は保存しない。';
