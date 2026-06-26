-- subscriptions テーブルへ service_role の DML 権限を明示的に付与する。
--
-- 20260624081031_baseline_subscriptions.sql では `grant all ... to service_role` を
-- 使用しているが、Supabase Cloud では `grant all` が service_role への DML 権限を
-- 確実に付与しない場合がある。stripe_customers / stripe_webhook_events で用いた
-- 明示的な GRANT パターンと統一し、本番環境での 42501 を防ぐ。
--
-- anon / authenticated への DML 権限は付与しない。
-- RLS ポリシーおよび既存の grant select to authenticated は変更しない。

GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLE public.subscriptions
  TO service_role;
