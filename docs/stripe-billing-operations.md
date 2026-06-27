# Marketcast Lab 課金運用・復旧手順書

> **重要事項（最優先）**
>
> - **Stripeが課金状態の正本。** 疑問が生じたときは必ずStripeの状態を確認してから判断する。
> - **Authユーザーを先に削除しない。** 必ずStripe Subscriptionの解約確認後にAuth削除を行う。
> - **Stripe Customerは原則削除しない。** 課金記録・会計証跡を保持するため。
> - **DBを直接変更しない。** 原則としてStripe Event再送によりDBを収束させる。
> - **`current_period_end`だけで重複契約の正本を決めない。**
> - **Stripeのみ残った契約はSupabase SQLだけでは検出できない。**
> - **migration適用前に対応Functionをdeployしない。**
> - **Live modeでテスト操作をしない。** 常にTest modeで操作する。

---

## 目次

1. [目的と対象範囲](#1-目的と対象範囲)
2. [基本原則](#2-基本原則)
3. [StripeとSupabaseの役割](#3-stripeとsupabaseの役割)
4. [データモデル概要](#4-データモデル概要)
5. [日常確認](#5-日常確認)
6. [outcome別対応](#6-outcome別対応)
7. [Webhook障害対応](#7-webhook障害対応)
8. [Stripe／DB不整合対応](#8-stripedb不整合対応)
9. [unresolved_user復旧手順](#9-unresolved_user復旧手順)
10. [duplicate_subscription復旧手順](#10-duplicate_subscription復旧手順)
11. [Authユーザー削除手順](#11-authユーザー削除手順)
12. [顧客問い合わせ対応](#12-顧客問い合わせ対応)
13. [DB直接操作の禁止事項](#13-db直接操作の禁止事項)
14. [復旧完了条件](#14-復旧完了条件)
15. [エスカレーション条件](#15-エスカレーション条件)
16. [将来の自動化候補](#16-将来の自動化候補)
17. [異常検出SQL集](#17-異常検出sql集)
18. [Stripe Dashboard確認手順](#18-stripe-dashboard確認手順)

---

## 1. 目的と対象範囲

本手順書はMarketcast Labの課金システム（Stripe + Supabase）の日常運用・異常検知・手動復旧方法を定めたものです。

**対象：**
- Stripe WebhookによるSupabaseへの課金状態同期
- Webhookの異常outcome（unresolved_user / duplicate_subscription）への対応
- Authユーザー削除時の課金状態の確認と処理
- SupabaseとStripeの不整合の検知と復旧

**対象外：**
- Stripe料金プランの変更
- Supabase Edge Functionのコード変更
- DB migration
- 新機能の追加

---

## 2. 基本原則

1. **Stripeを課金状態の正本とする。** SupabaseのDBはStripe Webhookによって同期されるキャッシュであり、課金に疑問が生じたときは常にStripeの状態を確認してから判断する。
2. **DBを直接変更しない。** 原則として、StripeのSubscription状態・metadataを正しくした上でWebhookイベントを再発生させてDBを収束させる（詳細は [13. DB直接操作の禁止事項](#13-db直接操作の禁止事項)）。
3. **Authユーザーを先に削除しない。** SubscriptionのStripe側での解約確認前にAuthユーザーを削除してはならない（詳細は [11. Authユーザー削除手順](#11-authユーザー削除手順)）。
4. **Stripe Customerを削除しない。** Customerを削除するとInvoice履歴・課金証跡が参照不可になる。解約後も保持する。
5. **`current_period_end`だけで重複契約の正本を決めない。** 補助情報のひとつに過ぎない（詳細は [10. duplicate_subscription復旧手順](#10-duplicate_subscription復旧手順)）。
6. **Stripeのみに残った契約はSupabase SQLだけでは検出できない。** 別途Stripeの一覧との照合が必要（詳細は [11. Authユーザー削除手順](#11-authユーザー削除手順)）。
7. **migration適用前に対応Functionをdeployしない。** DB schemaとFunctionの整合性が壊れる。
8. **Live modeでテスト操作をしない。** Stripe操作は常にTest modeで行う。

---

## 3. StripeとSupabaseの役割

| システム | 役割 | 正本か |
|---|---|---|
| **Stripe** | 課金契約・金額・支払い状態の管理 | **課金状態の正本** |
| `public.subscriptions` | 現在の契約状態のキャッシュ（Webhook経由で同期） | Stripeの副本 |
| `public.stripe_customers` | StripeCustomer ↔ SupabaseユーザーIDの対応表 | Stripeの副本 |
| `public.stripe_webhook_events` | Webhookの処理記録・冪等性管理 | 監査ログ |

---

## 4. データモデル概要

### subscriptions テーブル

| 列 | 型 | 備考 |
|---|---|---|
| `user_id` | UUID (PK) | 1ユーザー1行。FK → `auth.users(id) ON DELETE CASCADE` |
| `stripe_customer_id` | text UNIQUE NULL | NULL許容（UNIQUEだがNULLは重複可） |
| `stripe_subscription_id` | text UNIQUE NULL | 同上 |
| `status` | text NOT NULL | `incomplete` / `incomplete_expired` / `trialing` / `active` / `past_due` / `canceled` / `unpaid` / `paused` |
| `current_period_end` | timestamptz NULL | NULLは異常ではない（初期状態等） |
| `cancel_at_period_end` | boolean NOT NULL DEFAULT false | trueでも`current_period_end`が未来なら有料アクセス許可 |
| `price_id` | text NULL | |
| `created_at` / `updated_at` | timestamptz | `updated_at`はBEFORE UPDATEトリガーで自動更新 |

**設計原則：** 行なし = free（未契約）。`status='free'`は保存しない。`status='canceled'`行は削除しない（証跡保持）。

**RLS：** `authenticated`ロールは自分の行のSELECTのみ。`service_role`はバイパス。

### stripe_customers テーブル

| 列 | 型 | 備考 |
|---|---|---|
| `user_id` | UUID (PK) | FK → `auth.users(id) ON DELETE CASCADE` |
| `stripe_customer_id` | text UNIQUE NOT NULL | 1 Customer = 1 user を強制 |
| `created_at` / `updated_at` | timestamptz | |

**RLS：** `anon` / `authenticated` は deny_all。`service_role`のみ操作可。

### stripe_webhook_events テーブル

| 列 | 型 | 備考 |
|---|---|---|
| `event_id` | text (PK) | Stripe Event ID。冪等性管理のキー |
| `event_type` | text NOT NULL | `customer.subscription.updated` 等 |
| `outcome` | text NOT NULL DEFAULT 'applied' | `applied` / `ignored` / `unresolved_user` / `duplicate_subscription` |
| `stripe_customer_id` | text NULL | |
| `stripe_subscription_id` | text NULL | |
| `error_code` | text NULL | 将来拡張用。現在は常にNULL |
| `processed_at` / `created_at` | timestamptz | |

**RLS：** `anon` / `authenticated` は deny_all。`service_role`のみ操作可。

**重要：** 処理成功行のみ記録する（「記録at終了方式」）。処理失敗・Webhook配信失敗はDBに記録されない。

### 有料アクセス認可条件

以下をすべて満たすときに有料アクセスを許可する：

1. `subscriptions`行が存在する
2. `status` が `active` または `trialing`
3. `current_period_end` が NULL でない
4. `current_period_end` が現在時刻より未来（厳密：等しい場合も拒否）
5. `cancel_at_period_end = true` でも`current_period_end`が未来なら**許可**（仕様）

### Customer Portal の実装注意

`create-customer-portal-session` は `stripe_customers` テーブルではなく **`subscriptions.stripe_customer_id`** を参照する。`subscriptions`行がない、または`stripe_customer_id`がNULLの場合は404を返す。

---

## 5. 日常確認

### 毎日確認（所要目安：5分）

**① Stripe Dashboard の Webhook 配信確認**

Stripe Dashboard > Developers > Webhooks > エンドポイント選択 > Recent deliveries

- 赤（配信失敗）の行がないか確認
- 失敗があれば → [7. Webhook障害対応](#7-webhook障害対応)

**② 異常outcome SQL（直近24時間）**

Supabase Dashboard > SQL Editor で実行：

```sql
SELECT
  outcome,
  event_type,
  COUNT(*) AS cnt,
  MIN(processed_at) AS first_seen,
  MAX(processed_at) AS last_seen
FROM public.stripe_webhook_events
WHERE outcome IN ('unresolved_user', 'duplicate_subscription')
  AND processed_at >= now() - INTERVAL '24 hours'
GROUP BY outcome, event_type
ORDER BY outcome, cnt DESC;
```

- 0行 → 正常
- `unresolved_user` の行あり → [9. unresolved_user復旧手順](#9-unresolved_user復旧手順)
- `duplicate_subscription` の行あり → [10. duplicate_subscription復旧手順](#10-duplicate_subscription復旧手順)

### 毎週確認（所要目安：15分）

以下のSQLを順に実行。各SQLの詳細と判断基準は [17. 異常検出SQL集](#17-異常検出sql集) を参照。

- SQL-04: active/trialing期限切れ
- SQL-05: cancel_at_period_end矛盾
- SQL-06: Stripe ID欠落
- SQL-09: stripe_customersとsubscriptionsの不一致
- SQL-10: Webhookイベント量・outcome集計（週次サマリー）

---

## 6. outcome別対応

### applied

**意味：** Webhookが正常に処理され、DBに同期された。

**日常の対応：** 通常不要。

**補足：**
- Deploy前（旧コード処理済み）の行は `stripe_customer_id` / `stripe_subscription_id` がNULLの場合あり。新Deployコード以降はStripe IDが必ず記録される。
- `outcome='applied'`でもStripe IDがNULLな行は旧コード処理済みを示す正常な状態。

---

### ignored

**意味：** WebhookがDB更新の対象外だったイベント。

| 原因 | event_type | 正常 or 調査対象 |
|---|---|---|
| 未対応イベント（`payment_intent.*`等） | 各種 | 正常 |
| Checkout mode が `subscription` 以外 | `checkout.session.completed` | 正常 |
| Invoice に subscription ID なし | `invoice.paid` / `invoice.payment_failed` | **調査対象** |
| `default:` ケース | 上記以外 | 正常 |

**調査が必要な条件：** `invoice.paid` または `invoice.payment_failed` かつ `stripe_subscription_id IS NULL` → Stripe側でSubscription外の請求が発生している可能性。Stripe Dashboard でInvoiceを確認する。

---

### unresolved_user

**意味：** user_idの解決に失敗し、DBを更新しなかった。課金はされているがサービス利用不可（有料機能403）。

**対応：** [9. unresolved_user復旧手順](#9-unresolved_user復旧手順) を実行する。

---

### duplicate_subscription

**意味：** 同一ユーザーにbloking状態の別Subscriptionが既存するため、incoming Subscriptionを無視した。二重課金の可能性あり。

**対応：** [10. duplicate_subscription復旧手順](#10-duplicate_subscription復旧手順) を実行する。

---

## 7. Webhook障害対応

| 障害ケース | 確認先 | 対処 |
|---|---|---|
| 5xxで失敗 | Stripe > Developers > Webhooks > deliveries の HTTP status、Supabase > Functions > Logs でエラー内容 | 原因を解消後、Stripe Dashboard から手動Resend |
| 400 "Invalid signature" | STRIPE_WEBHOOK_SECRET の設定値 | Supabase Functions Secrets で正しい Signing Secret を設定 |
| 405 | Function URL の設定 | Stripe Dashboard > Webhooks で正しいURLを確認 |
| Stripeの自動再送（72時間以内） | Stripe > deliveries の再送履歴 | 待機のみ。自動再送で成功すれば自動解決 |
| 3日超の欠落 | Stripe > Events で event_id を検索 | Stripe Dashboard > Events > 該当イベント > Resend（Stripe内の"Resend"ボタン） |
| Edge Function停止 | Supabase > Functions でデプロイ状況確認 | migration適用確認後にdeploy |
| DB障害（SELECT/INSERT失敗） | Supabase Status page | DB復旧後はStripe自動再送を待つ |
| Stripe API障害 | Stripe Status page | Stripe API復旧後に自動再送で解決 |
| 同一event_idの再送 | stripe_webhook_events に同行が存在する | 正常動作（冪等性チェックでskip、200を返す） |
| 23505（INSERT重複） | 同上 | 正常動作（並列配信の競合、200を返す） |

**5xxの主要なエラーメッセージと原因：**

| HTTPレスポンスのメッセージ | 原因 |
|---|---|
| `"Service unavailable"` | 冪等性SELECTでDB障害 |
| `"Processing failed"` | handleEvent内でのthrow（Stripe API障害含む） |
| `"Event record failed"` | 業務処理後のINSERT失敗（migration未適用等） |

---

## 8. Stripe／DB不整合対応

| 不整合の症状 | 検出SQL | 対処 |
|---|---|---|
| active行だが期限切れ | SQL-04 | 最新Subscriptionイベント（`customer.subscription.updated`等）をStripe DashboardからResend |
| cancel_at_period_end が食い違う | SQL-05 | 最新SubscriptionイベントをResend |
| Stripe IDが欠落している | SQL-06 | Stripe DashboardでSubscriptionを確認しmetadataを設定後にResend |
| stripe_customersとsubscriptionsの不一致 | SQL-09 | [9. unresolved_user復旧手順](#9-unresolved_user復旧手順)またはケース別に対処 |
| UNIQUE制約が重複 | SQL-07 | **緊急。即時エスカレーション** |

---

## 9. unresolved_user復旧手順

### 発生原因

| 原因 | 発生条件 |
|---|---|
| Subscription metadataに`supabase_user_id`なし | Stripe Dashboard等で手動作成 |
| Customer metadataにも`supabase_user_id`なし | 上記かつCustomerも手動作成 |
| `stripe_customers`に対応行なし | DB同期前の既存Customer |
| 外部システムからの作成 | `supabase_user_id`が設定されない |

### 顧客影響

課金はされているが、DBが更新されず有料機能が403で利用不可。

### 復旧手順

#### Step 1: 対象を特定する

```sql
-- SQL-01: unresolved_user一覧（Section 17参照）
SELECT event_id, event_type, stripe_customer_id, stripe_subscription_id, processed_at
FROM public.stripe_webhook_events
WHERE outcome = 'unresolved_user'
ORDER BY processed_at DESC;
```

`stripe_customer_id` と `stripe_subscription_id` を控える。

#### Step 2: Stripe上でCustomer・Subscriptionを確認する

Stripe Dashboard > Customers > `stripe_customer_id` を検索：

- Customer の **metadata** に `supabase_user_id` があるか確認
- Customer > Subscriptions > 該当Subscription の **metadata** に `supabase_user_id` があるか確認

#### Step 3: 正しいuser_idを特定する

以下をすべて照合して正しいuser_id（Supabase Auth UUID）を確定する：

- Stripe Customer の email と Supabase Auth の email が一致するか
- Subscriptionの課金開始日がAuthユーザーの登録日より後か
- 別のuser_idが同じcustomer_idに既に紐付いていないか（SQL-09）
- 候補が複数ある場合は**開発責任者へエスカレーション**し、推測で決めない

#### Step 4: Stripe SubscriptionのmetadataにsupabaseユーザーIDを設定する

> **注意：** Customer metadataの変更はWebhookを発行しない。**Subscription metadataを変更する**。

Stripe Dashboard > Customers > 該当Customer > Subscriptions > 該当Subscription > Edit metadata

```
supabase_user_id = <確定したSupabase Auth UUID>
```

#### Step 5: 新しいWebhookイベントを発生させる

Subscriptionのmetadata更新により `customer.subscription.updated` イベントが自動で発行される。Stripe Dashboard > Developers > Events で確認する。

新しいイベントが発行されない場合の代替手段（第二選択）：
- Stripe Dashboard > Developers > Events で最新の `customer.subscription.updated` イベントを探し、"Resend" ボタンで再送する

#### Step 6: 復旧を確認する

```sql
-- stripe_webhook_events に新しい applied 行が記録されたか確認
SELECT event_id, outcome, stripe_customer_id, stripe_subscription_id, processed_at
FROM public.stripe_webhook_events
WHERE stripe_customer_id = '<対象のcustomer_id>'
ORDER BY processed_at DESC
LIMIT 5;

-- subscriptions 行が作成・更新されたか確認
SELECT user_id, status, stripe_customer_id, stripe_subscription_id, current_period_end
FROM public.subscriptions
WHERE stripe_customer_id = '<対象のcustomer_id>';
```

`outcome='applied'` の新行が記録され、`subscriptions`行が正しい状態になっていれば復旧完了。

### やってはいけない操作

- 推測でuser_idを決めない
- 候補が複数ある場合は作業を止め開発責任者へエスカレーション
- `subscriptions`に直接INSERTしない（後続Webhookで状態が壊れる）
- 過去EventのResendを第一選択にしない（新しいイベント発生が推奨）

---

## 10. duplicate_subscription復旧手順

### 発生原因

| 原因 | 発生条件 |
|---|---|
| Stripe Dashboard手動作成 | 同一Customerに複数Subscription |
| Checkout並列操作 | 二重チェックの隙間での同時リクエスト |
| 旧Subscriptionの解約漏れ | blocking status（active等）のSubが残存 |

### 顧客影響

incoming Subscriptionが無視される。二重課金の可能性あり。

### 復旧手順

#### Step 1: 対象を特定する

```sql
-- SQL-02: duplicate_subscription一覧（Section 17参照）
SELECT event_id, event_type, stripe_customer_id, stripe_subscription_id, processed_at
FROM public.stripe_webhook_events
WHERE outcome = 'duplicate_subscription'
ORDER BY processed_at DESC;
```

`stripe_subscription_id`（incomingの重複側）と `stripe_customer_id` を控える。

#### Step 2: DBに保存されているSubscriptionを確認する

```sql
SELECT user_id, status, stripe_subscription_id, stripe_customer_id, current_period_end, cancel_at_period_end
FROM public.subscriptions
WHERE stripe_customer_id = '<対象のcustomer_id>';
```

#### Step 3: Stripe DashboardでSubscriptionを確認する

Stripe Dashboard > Customers > 該当Customer > Subscriptions

両方のSubscription（stored と incoming）について確認：
- `status`
- `current_period_end`
- `cancel_at_period_end`
- `metadata.supabase_user_id`
- Invoiceの支払い状況
- Subscription作成日時

#### Step 4: 正本を判断する

以下の優先順序で正本を判断する。上位の条件で決定できた場合は下位の条件を参照しない。

| 優先度 | 判断基準 |
|---|---|
| 1 | 顧客本人が意図した契約か（顧客確認が必要な場合は問い合わせを確認） |
| 2 | Marketcast Labの正規Checkout経由か（`checkout.session.completed`で`client_reference_id`がある） |
| 3 | Subscription metadataまたはCustomer metadataの`supabase_user_id`が正しいか |
| 4 | 支払済みInvoice・課金金額が存在するか |
| 5 | Subscription作成日時（新しい方が最新の契約意図を示す） |
| 6 | 現在DBに採用されているSubscription（`subscriptions.stripe_subscription_id`） |
| 7 | `current_period_end`（補助情報。単独では判断しない） |

**上記で判断できない場合は、どちらのSubscriptionも操作せず開発責任者へエスカレーションする。**

#### Step 5: 不要なSubscriptionをキャンセルする

正本以外のSubscriptionを Stripe Dashboard でキャンセルする（顧客への影響を考慮し、原則「Cancel at period end」を選択）。

両方のSubscriptionに課金済みInvoiceがある場合は、重複期間の二重課金を確認し返金を検討する（Stripe > Payments > Refund）。

#### Step 6: キャンセルのWebhook処理を確認する

```sql
SELECT event_id, event_type, outcome, stripe_subscription_id, processed_at
FROM public.stripe_webhook_events
WHERE stripe_subscription_id = '<キャンセルしたSubscription ID>'
ORDER BY processed_at DESC
LIMIT 5;

SELECT user_id, status, stripe_subscription_id
FROM public.subscriptions
WHERE user_id = '<対象のuser_id>';
```

### やってはいけない操作

- 両方のSubscriptionを同時にキャンセルしない（正しい方まで解約してしまう）
- `current_period_end`だけで正本を決めない
- 判断できない場合にどちらかを操作しない（エスカレーション）
- 先にsubscriptions DBを直接変更しない

---

## 11. Authユーザー削除手順

### 重要前提：Auth削除後のStripe孤立契約はSupabase SQLでは検出できない

`auth.users`削除時はON DELETE CASCADEにより`subscriptions`・`stripe_customers`行も自動削除される。これにより **Supabase SQL（SQL-08）でStripeのみに残った契約を検出することは不可能**。

SQL-08（Auth外部キー整合性確認）は「CASCADEが正常に機能したか」の確認用であり、「Stripeのみ残存する契約の検出SQL」ではない。

Stripeのみに残存する契約の検出は次の方法で行う：
1. Stripe Dashboard > Customers で対象CustomerのactiveなSubscriptionを確認（検索・手動照合）
2. Subscription metadata の `supabase_user_id` が Supabase Auth に存在するか照合
3. 現状は手動照合のみ（将来的なReconciliation自動化の対象）

### 削除前必須チェック順序

```
1. Supabase Dashboard > Auth > Users で対象ユーザーのuser_idを確認
2. subscriptionsテーブルの現在の状態を確認（以下のSQL）
3. stripe_customer_idがある場合 → Stripe DashboardでSubscription状態を確認
```

```sql
-- 削除前の状態確認（SELECT のみ）
SELECT
  s.user_id,
  s.status,
  s.stripe_customer_id,
  s.stripe_subscription_id,
  s.current_period_end,
  s.cancel_at_period_end,
  sc.stripe_customer_id AS customers_customer_id
FROM public.subscriptions s
LEFT JOIN public.stripe_customers sc ON s.user_id = sc.user_id
WHERE s.user_id = '<対象のuser_id>';
```

### Subscription状態別の削除前処理

| Subscriptionの状態 | 削除前の処理 |
|---|---|
| 行なし（free） | そのままAuth削除可 |
| `status = 'canceled'` または `'incomplete_expired'` | そのままAuth削除可 |
| `status = 'active'` かつ `cancel_at_period_end = true` | 期末解約済み。`current_period_end`後に削除するか、期末より前に削除する場合はStripe DashboardでCancel now |
| `status = 'active'` かつ `cancel_at_period_end = false` | **Stripe DashboardでSubscriptionをCancel at period end またはCancel nowしてから削除** |
| `status = 'trialing'` / `'past_due'` / `'unpaid'` / `'paused'` | Stripe側での状態確定を先に行ってから削除 |
| `status = 'incomplete'` | Checkout未完了。Stripeで自動的に`incomplete_expired`になるのを待つか、Stripe DashboardでキャンセルしてからAuth削除 |

### 削除の安全な順序

```
1. Stripe DashboardでSubscriptionをキャンセル
2. customer.subscription.deleted Webhookの処理完了を確認
   （stripe_webhook_eventsにapplied行が記録される）
3. subscriptions.status = 'canceled' になったことを確認
4. Supabase Auth > Users でユーザーを削除
   → ON DELETE CASCADEでsubscriptions / stripe_customersが自動削除される
```

### 誤削除時の対処

Auth削除は**不可逆**。同じAuth UUIDで再作成することは不可能。

誤って先にAuthを削除した場合：
1. Stripe DashboardでSubscriptionを確認（Customerは残存）
2. active/trialingのSubscriptionがある場合 → Stripe DashboardでCancel now
3. 課金記録はStripe側に保持される（Customerを削除しない）
4. 必要に応じて該当期間の返金を検討（Stripe > Payments > Refund）

### 将来の退会機能の必須要件

自動退会フローを実装する場合は以下を満たすこと：

1. Stripe API でSubscriptionをキャンセル（billing_cycleまたは即時）
2. キャンセル確認後にAuth削除
3. Stripe Customerは削除しない
4. 失敗時のロールバック設計（Stripe APIエラー時にAuthを削除しない）
5. 退会確認UI（Cancel at period end か Cancel now かの選択）
6. データ保持ポリシーの明示

---

## 12. 顧客問い合わせ対応

| 問い合わせ内容 | 最初に確認するもの | 次のアクション |
|---|---|---|
| 「課金されているのに機能が使えない」 | SQL-01でoutcome確認 + Stripe DashboardでSubscription確認 | outcome='unresolved_user' → [Section 9](#9-unresolved_user復旧手順)、active/trialing期限切れ → SQL-04後にSection 8 |
| 「解約したはずなのにまだ課金されている」 | Stripe DashboardでSubscriptionの`cancel_at_period_end`と次回課金日を確認 | `cancel_at_period_end=true`なら期末まで利用可能と案内。即時解約を希望なら[Section 11](#11-authユーザー削除手順)のCancel nowを参照 |
| 「二重に課金されている」 | Stripe > Customer > Invoices で確認 | [Section 10](#10-duplicate_subscription復旧手順)の手順を実行し、必要なら返金 |
| 「退会したのに課金が続いている」 | Auth削除前のStripe解約確認の有無を確認 | [Section 11](#11-authユーザー削除手順)の「誤削除時の対処」を参照 |
| 「プランのキャンセルを予約したが表示がない」 | subscriptionsの`cancel_at_period_end`とStripeの状態を比較 | SQL-05b を実行。不一致なら最新イベントをResend |

---

## 13. DB直接操作の禁止事項

### 禁止操作（条件なし）

| テーブル | 禁止操作 | 理由 |
|---|---|---|
| `stripe_webhook_events` | INSERT / UPDATE / DELETE | 冪等性管理の破壊。監査ログの改ざん |
| `subscriptions` | INSERT | 後続Webhookで上書きされず二重行になる可能性 |
| `subscriptions` | DELETE | 証跡消失。「行なし=free」設計の混乱 |
| `stripe_customers` | INSERT / UPDATE / DELETE | Customer ↔ User対応の整合性破壊 |

### 条件付き許容操作（最終手段のみ）

`subscriptions` への直接UPDATEは以下をすべて満たす場合のみ許容する：

1. Stripe側のイベント再送・新イベント発生で解決できない（例：イベントが72時間超で再送不可かつStripe側でイベントを再発生させられない）
2. Stripeの現在の状態が正確に把握できている
3. 変更前後の値を記録媒体（Slack等）に保存した
4. 変更後に到着するWebhookが正しい値に上書きすることを確認した
5. **開発責任者の承認を得た**

### 直接UPDATE前に必ず保存する情報

```sql
-- 変更前の現在値（結果をコピーして保存）
SELECT * FROM public.subscriptions WHERE user_id = '<対象UUID>';
SELECT * FROM public.stripe_customers WHERE user_id = '<対象UUID>';
SELECT event_id, event_type, outcome, processed_at
FROM public.stripe_webhook_events
WHERE stripe_customer_id = '<対象CustomerID>'
ORDER BY processed_at DESC
LIMIT 10;
```

加えてStripe DashboardのSubscription状態をスクリーンショット保存する。

### 復旧の優先順序

```
1. StripeのSubscription/metadata状態を正しくする
   └ Subscription metadata に supabase_user_id を設定する
   └ 不要な Subscription をキャンセルする
2. 新しいStripe Subscriptionイベントを発生させる
   └ metadata更新により customer.subscription.updated が自動発行される
   └ 発生しない場合は Stripe Dashboard から Resend（第二選択）
3. Webhookが applied で処理されDBが同期されたことを確認する
4. 上記で解決しない場合のみ開発責任者に相談しDB直接更新を最終手段として検討する
```

---

## 14. 復旧完了条件

以下をすべて確認した場合に復旧完了とする：

1. `stripe_webhook_events` に `outcome='applied'` の新行が記録されている
2. `subscriptions` の状態がStripe Dashboardの状態と一致している
3. 有料ユーザーが有料機能にアクセスできる（必要な場合に確認）
4. Stripe Webhookの次回配信が正常（2xx）を返している
5. 二重課金があった場合は返金処理が完了している

---

## 15. エスカレーション条件

以下の場合は個人判断での対処をせず、開発責任者に報告する：

- UNIQUE制約違反が発生した（SQL-07で行が検出された）
- Stripe側で予期しない課金・返金が発生した
- Auth削除後にStripeのみに課金が残存していることが判明した
- 複数ユーザーへの課金状態影響が確認された
- DBとStripeの不整合がStripe Event再送で解決しない
- unresolved_user復旧において正しいuser_idが特定できない
- duplicate_subscriptionの正本判断ができない

---

## 16. 将来の自動化候補

### 優先度: Should（公開直後に整備）

- **異常outcome通知：** 毎日のSQL-01/02相当をSupabase Edge Function（スケジュール実行）で自動実行し、結果があればSlack/メールへ通知する。
- **Webhook失敗通知：** Stripe Dashboard > Developers > Webhooks > エンドポイント > 通知設定を有効化（Stripe側の設定のみ）。

### 優先度: Later（安定稼働後・必要性に応じて）

- **Reconciliation Function（手動起動）：** `subscription_id`を指定してStripeから最新状態を取得し`subscriptions`へupsertする管理者向けFunction。管理者ロール（admin role）の実装が前提条件。
- **Stripe ↔ Supabase 定期自動照合：** Stripe APIのSubscription一覧とDBを定期比較し不整合を検出するスケジュール実行Function。Stripe側にのみ残存するSubscriptionの自動検出が可能になる。
- **監査ログ拡充：** `stripe_webhook_events`に処理時間・再送回数・処理者を追加する。

---

## 17. 異常検出SQL集

> **すべてSELECT（読み取り専用）。** INSERT / UPDATE / DELETE は実行しない。
>
> Supabase Dashboard > SQL Editor で実行する。
> 実Customer ID・Subscription ID・user_idは本手順書には記載しない。

---

### SQL-01: unresolved_user 一覧

**目的：** user_id解決に失敗したWebhookイベントの検出。課金済みだがDB未同期のユーザーを特定する。

**実行頻度：** 毎日

**正常時の結果：** 0行

**異常時の次の行動：** [9. unresolved_user復旧手順](#9-unresolved_user復旧手順) を実行する。

```sql
SELECT
  event_id,
  event_type,
  stripe_customer_id,
  stripe_subscription_id,
  processed_at,
  created_at
FROM public.stripe_webhook_events
WHERE outcome = 'unresolved_user'
ORDER BY processed_at DESC;
```

---

### SQL-02: duplicate_subscription 一覧

**目的：** 同一ユーザーへの複数Subscription衝突の検出。二重課金リスクの確認。

**実行頻度：** 毎日

**正常時の結果：** 0行

**異常時の次の行動：** [10. duplicate_subscription復旧手順](#10-duplicate_subscription復旧手順) を実行する。

```sql
SELECT
  event_id,
  event_type,
  stripe_customer_id,
  stripe_subscription_id,
  processed_at,
  created_at
FROM public.stripe_webhook_events
WHERE outcome = 'duplicate_subscription'
ORDER BY processed_at DESC;
```

---

### SQL-03: 直近24時間の異常outcome

**目的：** 直近の異常集計。定期モニタリング用。

**実行頻度：** 毎日

**正常時の結果：** 0行

**異常時の次の行動：** outcome種別に応じてSQL-01またはSQL-02を実行し詳細を確認する。

```sql
SELECT
  outcome,
  event_type,
  COUNT(*) AS cnt,
  MIN(processed_at) AS first_seen,
  MAX(processed_at) AS last_seen
FROM public.stripe_webhook_events
WHERE outcome IN ('unresolved_user', 'duplicate_subscription')
  AND processed_at >= now() - INTERVAL '24 hours'
GROUP BY outcome, event_type
ORDER BY outcome, cnt DESC;
```

---

### SQL-04: active/trialing の期限切れ矛盾

**目的：** `active`または`trialing`のまま`current_period_end`が過去になっている行の検出。Webhookが長期間届いていない可能性。

**実行頻度：** 毎週

**正常時の結果：** 0行

**異常時の次の行動：** `stripe_subscription_id`でStripe DashboardのSubscription状態を確認し、最新の`customer.subscription.updated`または`invoice.paid`イベントをResendする。

```sql
SELECT
  user_id,
  status,
  current_period_end,
  cancel_at_period_end,
  stripe_subscription_id,
  updated_at
FROM public.subscriptions
WHERE status IN ('active', 'trialing')
  AND current_period_end IS NOT NULL
  AND current_period_end <= now()
ORDER BY current_period_end ASC;
```

---

### SQL-05: cancel_at_period_end 矛盾

**目的：** 解約予定フラグに関わる不整合の検出。

**実行頻度：** 毎週

**正常時の結果：** すべて0行

**異常時の次の行動：** 最新の`customer.subscription.updated`イベントをStripe DashboardからResendする。

```sql
-- 5a: cancel_at_period_end=true だが current_period_end が NULL
SELECT user_id, status, cancel_at_period_end, current_period_end, stripe_subscription_id
FROM public.subscriptions
WHERE cancel_at_period_end = true
  AND current_period_end IS NULL;

-- 5b: cancel_at_period_end=true かつ active/trialing なのに期限切れ
SELECT user_id, status, cancel_at_period_end, current_period_end, stripe_subscription_id
FROM public.subscriptions
WHERE cancel_at_period_end = true
  AND status IN ('active', 'trialing')
  AND current_period_end IS NOT NULL
  AND current_period_end <= now();

-- 5c: status=canceled だが cancel_at_period_end=true のまま（クリアされていない）
SELECT user_id, status, cancel_at_period_end, current_period_end, stripe_subscription_id
FROM public.subscriptions
WHERE status = 'canceled'
  AND cancel_at_period_end = true;
```

---

### SQL-06: Stripe ID 欠落

**目的：** `subscriptions`行が存在するがStripe IDが未記録の異常を検出。

**実行頻度：** 毎週

**正常時の結果：** 0行（現行コードでは常にIDが記録される）

**異常時の次の行動：** Stripe DashboardでCustomer/Subscriptionを確認。metadataが正しければWebhook Resendで解決。解決しない場合はエスカレーション。

```sql
SELECT
  user_id,
  status,
  stripe_customer_id,
  stripe_subscription_id,
  current_period_end,
  created_at,
  updated_at
FROM public.subscriptions
WHERE stripe_customer_id IS NULL
   OR stripe_subscription_id IS NULL;
```

---

### SQL-07: subscriptions 内の Stripe ID 重複

**目的：** UNIQUE制約で通常は発生しないが、制約回避後の不整合確認。

**実行頻度：** 問い合わせ時・障害後

**正常時の結果：** 0行

**異常時の次の行動：** DBの整合性が壊れている可能性があるため**即時エスカレーション**。

```sql
-- 7a: stripe_customer_id 重複（subscriptions）
SELECT stripe_customer_id, COUNT(*) AS cnt, array_agg(user_id) AS user_ids
FROM public.subscriptions
WHERE stripe_customer_id IS NOT NULL
GROUP BY stripe_customer_id
HAVING COUNT(*) > 1;

-- 7b: stripe_subscription_id 重複（subscriptions）
SELECT stripe_subscription_id, COUNT(*) AS cnt, array_agg(user_id) AS user_ids
FROM public.subscriptions
WHERE stripe_subscription_id IS NOT NULL
GROUP BY stripe_subscription_id
HAVING COUNT(*) > 1;

-- 7c: stripe_customer_id 重複（stripe_customers）
SELECT stripe_customer_id, COUNT(*) AS cnt, array_agg(user_id) AS user_ids
FROM public.stripe_customers
GROUP BY stripe_customer_id
HAVING COUNT(*) > 1;
```

---

### SQL-08: Auth 外部キー整合性確認

**目的：** ON DELETE CASCADEが正常に機能しているかの確認。Stripeのみに残った契約の検出SQLではない（その検出はStripe Dashboard側での照合が必要）。

**実行頻度：** 問い合わせ時・Authユーザー削除後

**正常時の結果：** 0行

**異常時の次の行動：** FK制約が無効化された可能性があるため即時エスカレーション。

```sql
-- 8a: subscriptions に auth.users に存在しない user_id がある
SELECT s.user_id, s.status, s.stripe_subscription_id
FROM public.subscriptions s
LEFT JOIN auth.users u ON s.user_id = u.id
WHERE u.id IS NULL;

-- 8b: stripe_customers に auth.users に存在しない user_id がある
SELECT sc.user_id, sc.stripe_customer_id
FROM public.stripe_customers sc
LEFT JOIN auth.users u ON sc.user_id = u.id
WHERE u.id IS NULL;
```

---

### SQL-09: stripe_customers と subscriptions の不一致

**目的：** 同一ユーザーでCustomer IDが食い違っている不整合の検出。

**実行頻度：** 毎週

**正常時の結果：** 9a・9b は0行。9c は正常（free ユーザーとして正常）。

**異常時の次の行動：**
- 9a（Customer ID不一致）: `create-customer-portal-session`がsubscriptionsのIDを使用するため影響あり。Stripe Dashboardで実際のCustomerを確認しmigration経緯を調査する。
- 9b（subscriptionsにあるがstripe_customersにない）: WebhookがStripe Customerを記録しなかった可能性。最新イベントのResendで解決することが多い。

```sql
-- 9a: 同一user_idで stripe_customer_id が異なる
SELECT
  s.user_id,
  s.stripe_customer_id AS sub_customer_id,
  sc.stripe_customer_id AS customers_customer_id,
  s.status,
  s.stripe_subscription_id
FROM public.subscriptions s
JOIN public.stripe_customers sc ON s.user_id = sc.user_id
WHERE s.stripe_customer_id IS NOT NULL
  AND s.stripe_customer_id != sc.stripe_customer_id;

-- 9b: subscriptions にあるが stripe_customers にない
SELECT
  s.user_id,
  s.stripe_customer_id,
  s.status
FROM public.subscriptions s
LEFT JOIN public.stripe_customers sc ON s.user_id = sc.user_id
WHERE sc.user_id IS NULL
  AND s.stripe_customer_id IS NOT NULL;

-- 9c: stripe_customers にあるが subscriptions にない（free ユーザーとして正常）
SELECT
  sc.user_id,
  sc.stripe_customer_id,
  sc.created_at
FROM public.stripe_customers sc
LEFT JOIN public.subscriptions s ON sc.user_id = s.user_id
WHERE s.user_id IS NULL;
```

---

### SQL-10: Webhook件数・outcome集計

**目的：** 配信量・異常率の定期把握。

**実行頻度：** 毎日・毎週

**正常時の結果：** `unresolved_user` / `duplicate_subscription` が0件。

**異常時の次の行動：** 増加傾向が見られる場合はSQL-01/02で詳細を確認する。

```sql
-- 直近24時間の集計
SELECT
  event_type,
  outcome,
  COUNT(*) AS cnt
FROM public.stripe_webhook_events
WHERE processed_at >= now() - INTERVAL '24 hours'
GROUP BY event_type, outcome
ORDER BY event_type, outcome;

-- 直近7日間のoutcome別集計
SELECT
  outcome,
  COUNT(*) AS total,
  COUNT(CASE WHEN processed_at >= now() - INTERVAL '24 hours' THEN 1 END) AS last_24h
FROM public.stripe_webhook_events
GROUP BY outcome
ORDER BY outcome;

-- 直近7日間の異常outcome推移（日次）
SELECT
  DATE_TRUNC('day', processed_at AT TIME ZONE 'Asia/Tokyo') AS day_jst,
  outcome,
  COUNT(*) AS cnt
FROM public.stripe_webhook_events
WHERE outcome IN ('unresolved_user', 'duplicate_subscription')
  AND processed_at >= now() - INTERVAL '7 days'
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
```

---

### SQLで検出できない事項（Stripe Dashboard での確認が必要）

| 検出したい事象 | 検出不可の理由 | 代替確認方法 |
|---|---|---|
| Webhookが配信されたが5xxを返した | 失敗時はDBに行が作られない | Stripe Dashboard > Webhooks > Event delivery |
| Stripe側でWebhook配信が失敗している | 同上 | Stripe Dashboard > deliveries のstatus |
| Functionの実行エラー | INSERT前のthrowは記録なし | Supabase Dashboard > Functions > Logs |
| Webhookイベントの欠落 | 受信ログなし | Stripe Event IDの連続性を目視確認 |
| **Stripeのみ残存するSubscription** | Auth削除でDB行が消えるため | Stripe Dashboard > Customers で照合 |

---

## 18. Stripe Dashboard確認手順

> **前提：** 本手順書の操作は Test mode を前提とする。Live mode では実行しない。

### Customer確認

1. Stripe Dashboard > Customers
2. 検索ボックスにCustomer ID（`cus_...`）またはemailを入力
3. 確認項目：email、metadata（`supabase_user_id`）、Subscriptions一覧

### Subscription確認

1. Customer詳細画面 > Subscriptions > 該当Subscription
   または Stripe Dashboard > Subscriptions で直接検索
2. 確認項目：status、`current_period_end`、`cancel_at_period_end`、metadata（`supabase_user_id`）、Items（price_id）

### Subscription metadata確認・更新

1. Subscription詳細画面 > "Edit" または metadata欄
2. `supabase_user_id` キーの有無と値を確認
3. 更新時：値を編集して保存 → `customer.subscription.updated` イベントが自動発行される

### Invoice・Payment確認

1. Customer詳細 > Invoices で請求一覧を確認
2. 各Invoiceで：支払い状況（paid/open/uncollectible）、金額、対象期間を確認
3. Stripe Dashboard > Payments で個別の決済詳細を確認

### Event・Webhook delivery確認

1. Stripe Dashboard > Developers > Events > Event IDを検索
   または > Webhooks > エンドポイント選択 > Recent deliveries
2. 各Eventで：event.id、event.type、発生日時（`event.created`）、data.objectを確認
3. deliveriesでは：HTTPステータス、レスポンスボディ、試行履歴を確認

### Event再送（Resend）

1. Stripe Dashboard > Developers > Events > 該当イベントを選択
2. ページ右上の "Resend" ボタンをクリック
   または deliveries の失敗行から "Resend" をクリック
3. 同一`event.id`で再送されるため、Supabase側では冪等性チェックでskipされる場合がある
   （既に処理済みの場合は正常）
4. 再送後、`stripe_webhook_events`に新行が記録されたか確認する

### Subscriptionの解約操作

- **Cancel at period end（推奨）：** Subscription > "Cancel subscription" > "Cancel at end of billing period"
  → `cancel_at_period_end=true`になり`customer.subscription.updated`が発行される。`current_period_end`まで利用可能。
- **Cancel now（即時）：** Subscription > "Cancel subscription" > "Cancel immediately"
  → Subscriptionがcanceledになり`customer.subscription.deleted`が発行される。未経過分の返金は別途要確認。

---

*本手順書はPhase E0-2「課金運用・復旧手順整備」として作成。コード変更なし・設計・文書化のみ。*
