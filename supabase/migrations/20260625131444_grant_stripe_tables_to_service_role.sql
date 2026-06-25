-- stripe_customers / stripe_webhook_events へ service_role の DML 権限を付与する。
--
-- Supabase の CREATE TABLE はデフォルトで service_role に SELECT/INSERT/UPDATE/DELETE を
-- 付与しない。subscriptions テーブルは 20260624081031 で明示的に grant all しているが、
-- 後続の 2 テーブルに同等の GRANT が漏れていたため 42501（insufficient_privilege）が発生した。
--
-- anon / authenticated への DML 権限は付与しない。
-- RLS の deny_all_non_service_role ポリシーも引き続き有効。

GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLE public.stripe_customers
  TO service_role;

GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLE public.stripe_webhook_events
  TO service_role;
