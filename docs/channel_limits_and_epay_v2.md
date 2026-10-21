# Channel daily limits and EPay V2 ingress

This extension guards V1 and V2 payment connector execution through the same PostgreSQL quota pools. It does not change payment connectors. The optional EPay gateway is an independent, RSA-only ingress service using the native V1 merchant payment API. It is not an outbound connector to another EPay platform.

## Channel quota execution

Apply `migrations/2026-10-20-000001_channel_daily_limits` in each tenant's payment database schema before enabling:

```toml
[platform]
channel_daily_limits_enabled = true
channel_daily_limits_require_binding = true # optional fail-closed admission for unbound channels
```

When disabled, existing payment traffic does not query the quota tables. When enabled, quota storage errors fail closed. Payment execution requires a resolved merchant connector account. External-vault proxy execution is explicitly refused while this flag is enabled because that alternate executor does not yet implement the reservation contract; there is no unguarded proxy fallback. V1 and V2 both reserve at the final connector execution point.

Quota policies are bound to `(merchant_id, merchant_connector_id, currency)`. A `pool_id` may deliberately be shared by multiple merchant connector accounts using the same upstream account. Limits, currency, timezone and enabled state belong to that pool. Disabling or changing the limit of a shared pool affects **all** its members.

An unbound channel keeps its existing behavior when `channel_daily_limits_require_binding` is false. Setting that flag true rejects every unbound charging account/currency before connector execution; a bound channel also rejects unconfigured currencies. To enforce limits for every channel, register every active account/currency pair before enabling the flag; enrolling a merchant in collection isolation does not itself provision quota policies. V2 uses the same final reservation and settlement checks, but does not yet exclude exhausted channels during its earlier route planning; a selected full channel is rejected before its charging call rather than silently re-routed. Read-only and refund flows do not reserve new capacity.

Both a platform administrative key and a tenant-level dashboard JWT with `TenantAccountWrite` permission may manage policies. Merchant keys and merchant dashboard roles cannot. Endpoints:

- `GET /configs/channel-limits`: bindings, pool policy and today's used amount, including reservations.
- `POST /configs/channel-limits`: create binding or update a pool's limit/enabled state.
- `POST /configs/channel-limits/reconcile`: resolve an uncertain reservation after independent reconciliation. Requires `merchant_id`, `merchant_connector_id`, `attempt_id`, `state` (`committed` or `released`) and a meaningful `reason` describing reconciliation evidence. Committed charges cannot be released.

The platform dashboard has a Channel daily limits panel on `/platform`. It converts the displayed currency amount into its minimum denomination using the existing currency precision definitions. For API calls, amounts are always integers in minimum denomination:

```json
{
  "merchant_id": "collection_a",
  "merchant_connector_id": "mca_replace_me",
  "pool_id": "upstream-account-a-usd",
  "currency": "USD",
  "daily_limit": 300000,
  "timezone": "Asia/Shanghai",
  "enabled": true
}
```

This means USD 3,000 per local calendar day. PostgreSQL validates the timezone. The pool's currency/timezone and a channel's pool association are immutable so changing configuration cannot reset existing usage. A zero limit closes admission; lowering a limit never removes existing reservations. Configuration changes and manual resolutions have an append-only application audit table; database operators must separately control table privileges and retention.

The CLI `scripts/channel_limits.py` reads `HYPERSWITCH_PLATFORM_TOKEN` or `HYPERSWITCH_ADMIN_KEY`, and optionally `HYPERSWITCH_API_URL`. Use `list`, `set --policy /private/policy.json`, or `resolve --policy /private/resolution.json`. Never place administrative keys in browser configuration.

### Accounting and failure semantics

V1 route planning excludes unavailable channels; V2 currently checks the selected channel only at final execution. Both atomically reserve the selected channel **before** connector preparation/calls. Each PostgreSQL pool row serializes concurrent admissions across processes and merchants. `(merchant_id, attempt_id, merchant_connector_id)` makes same-day reservation retries idempotent; changing the amount of an existing reservation is refused. Incremental authorization that increases the reserved amount is consequently refused for a managed channel.

The guard counts the total attempt amount, including amounts represented in the attempt's net total. Read-only sync and void flows do not acquire new reservations. Confirmed charged/partially charged results commit the full reservation conservatively. A positively confirmed void or definite authorization/authentication decline may release a reservation. Transport errors, ambiguous timeouts, 5xx, 408, 409, 425 and 429 keep the reservation. Refunds never restore a committed allowance. Partial capture keeps the original full reserved amount rather than releasing unused capacity prematurely.

A crash after reservation does not lose the hold. There is no automatic reservation TTL: abandonment and uncertain results must be reconciled, including results that arrive through update paths outside the normal connector completion hook. Conservative unreleased holds may reduce available capacity, but do not create extra capacity. PostgreSQL is the source of truth; Redis eviction/restart cannot reset usage.

Pending reservations carry forward across midnight and continue to consume today’s capacity. Confirmed charges are assigned to the local date on which the router records their successful result. Previous-day committed charges no longer consume today’s allowance. A previous-day committed attempt cannot issue a new charge/capture through the guard: multi-day partial captures on one attempt are refused, rather than bypassing the next day’s cap. This conservatively bounds admitted and observed charged volume, **not an upstream settlement-day guarantee**. Delayed notifications, upstream settlement cutoffs and timezone differences require upstream-specific reconciliation. Do not free pending authorizations at midnight or assume refunds restore the upstream limit.

Route planning is an availability check; the final reservation is authoritative. If another request consumes the remaining capacity between planning and reservation, the request is refused before the connector call. It can be retried through normal routing, but the router does not silently retry an already ambiguous upstream payment on another channel.

## RSA-only EPay V2 gateway

Files are in `services/epay_v2/`. Runtime dependencies are Python 3.11+ and OpenSSL with RSA support, not additional Python packages. Run behind an HTTPS reverse proxy; the built-in server binds loopback by default and bounds concurrent requests.

```text
EPay V2 client -> RSA verification -> merchant-bound native API
              -> native routing -> channel reservation -> existing connector
              -> authoritative payment status -> signed response / durable notification
```

Supported subset:

| Endpoint | Method | Behavior |
| --- | --- | --- |
| `/api/pay/submit` | GET/POST | Redirect to a native hosted payment link |
| `/api/pay/create` | POST | Return `pay_type=jump` and the native payment link |
| `/api/pay/query` | POST | Merchant-scoped order query; `status=1` only for native `succeeded` |
| `/api/pay/refund` | POST | Submit an idempotent full or partial native refund |
| `/api/pay/refundquery` | POST | Query/sync a merchant-owned native refund |

`submit` may omit `type` only when the merchant has a configured default or a single enabled type. `create` requires `type`. External order identifiers accept 1–64 ASCII letters, digits, underscores, dots and hyphens. `method` supports `web` and `jump`; jsapi/app/scan/applet, transfers and merchant-list APIs are not implemented and must not be advertised. Native payment methods/connectors still have to support the selected type. Default mappings are `alipay -> ali_pay` and `wxpay -> we_chat_pay`; an administrator may supply `payment_method_types` mappings in the private configuration. These become native `allowed_payment_method_types`, not routing overrides.

Only `sign_type=RSA` is accepted (omission implies RSA, never MD5). Requests require a ten-digit seconds timestamp within 300 seconds. Signatures use RSA PKCS#1 v1.5 + SHA-256, Base64 encoded. Nonempty scalar fields are sorted by ASCII parameter name and joined as raw UTF-8 `key=value` pairs using `&`. `sign` and `sign_type` are excluded. Whitespace-only values use the PHP SDK's empty-value convention. No shared secret is appended. Duplicate form keys and unsupported fields are refused.

The merchant signs requests with its private key; the gateway verifies against registered merchant public keys. Responses and notifications are signed with the platform private key. The merchant must verify the platform signature and timestamp. Multiple registered merchant public keys support overlap during rotation; platform key rotation must be coordinated with clients. Use RSA 2048, 3072 or 4096 bit keys and PEM key files. No V1 paths or MD5 downgrade are provided.

Protocol reference: [EPay PHP V2 SDK service class](https://github.com/OpenAegis/epay_sdk_php_v2/blob/main/lib/EpayCore.class.php). This repository is a published SDK copy, not verified as the original vendor. Its canonicalization and `orderStatus` behavior are the interoperability target; no third-party SDK code is executed by the service. Compatibility is limited to the subset above and the included tests, not a certification of every EPay fork.

### Configuration and execution

Copy `services/epay_v2/config.example.json` to a private directory, replace identifiers/keys, and set mode `0600`. Private keys must also be `0600`; keep the parent directory private. The service stores native API keys only in this configuration and accepts no such keys from clients. Platform and merchant private keys must never be stored in this repository.

```sh
python3 services/epay_v2/gateway.py --config /private/epay/gateway.json
```

Each numeric `pid` maps to exactly one native merchant/profile/API key. Native API keys must belong to the corresponding collection merchant. The currency is fixed by that mapping; `money` is never silently converted between currencies. The compatibility layer supports CNY/USD/EUR/HKD/GBP with two decimals. The channel quota API itself uses native minor units and is not limited to these currencies.

`public_url` is the externally reachable gateway base URL and is required for native hosted-checkout returns. `default_return_url` is required for callers omitting `return_url`. The native return points to an unguessable gateway order token. The gateway ignores browser-supplied status, queries native stored status and only redirects with RSA-signed `TRADE_SUCCESS` fields after authoritative success. An unconfirmed payment displays a waiting message without signing success. Returning to a merchant page does not replace asynchronous notification; merchants should use signed notifications or query before fulfilling orders. Callback URLs cannot already contain protocol parameter names because duplicate query keys would make verification ambiguous.

The included `client.py` is an RSA-only command-line client. Its private configuration contains `api_url`, `pid`, `merchant_private_key`, `platform_public_key`. It verifies platform signatures even on error responses. Use `create`, `query`, `refund`, or `refundquery` with a JSON parameters argument; never put private key contents in arguments.

### Refund API

`POST /api/pay/refund` takes exactly one of `trade_no` / `out_trade_no`, a positive two-decimal `money`, and preferably a unique `out_refund_no`, together with normal PID/RSA/timestamp fields. Refund IDs accept the same ASCII characters and length limit as external order IDs. No native merchant ID, connector credentials or routing options are accepted from clients. Refunds require a captured native payment (`succeeded` or `partially_captured`) and cannot exceed its captured amount or the original compatibility order amount.

`out_refund_no` is unique **per PID**, not per payment. The same ID/order/amount retries the original refund; different order or amount is refused. If omitted, a deterministic default permits only one implicit refund operation for that order. Additional partial refunds must use explicit new IDs. The returned platform `refund_no` is also the deterministic native `refund_id`, persisted before native submission. This native ID is always reused after crashes or lost replies; native uniqueness is established before connector execution.

`POST /api/pay/refundquery` takes exactly one of `refund_no` / `out_refund_no`. It checks the PID-scoped mapping and current native payment ownership, then uses native refund retrieval with `force_sync=true`. Refund results are checked against immutable payment ID, amount, currency and profile fields. Native V1 refund responses do not contain a merchant ID; this does not weaken the separate merchant/profile checks on payment responses.

Responses include `refund_no`, `out_refund_no`, `trade_no`, `out_trade_no`, `money`, `status`, `refund_status`, `addtime` (UTC), timestamp and RSA signature. Creation `code=0` means accepted, **not necessarily refunded**. Only `status=1` means confirmed success. `status=0` means confirmed failure; `status=2` is the explicit pending/uncertain extension, so asynchronous refunds are never mislabeled successful or failed. Query `code=0` means the query succeeded. Unknown creation outcomes return `code=-1` with `status=2` and the original refund identifiers; retry/query that same operation instead of generating a new refund number. No `reducemoney` balance/fee claim is fabricated: the gateway has no merchant balance or fee ledger.

### Reconciliation and external settlement register

`services/epay_v2/reconciliation.py` provides an offline, operator-only statement from **confirmed** gateway payments and refunds. It groups events by the gateway's recorded success timestamps, not the upstream processor's bank-settlement date. Amounts are integer minor units; pending/unknown results are excluded. Newly recorded orders preserve currency and newly confirmed refunds preserve their first success timestamp. A historical paid order without a verified currency or historical successful refund without a confirmation timestamp stops reporting until independently reconciled. This tool does not expose a merchant balance or initiate transfers.

With a private SQLite ledger, an operator may run `statement` for a timezone-aware half-open period, then `record` after checking an **external** bank/processor statement. `record` requires an external reference and evidence, stores gross charges, confirmed refunds, net amount, a SHA-256 event manifest, and the period in an immutable SQLite table. Overlapping periods for the same PID are rejected. The record asserts only that the operator matched an external settlement; it is not proof that the gateway held or transferred funds. Keep the ledger and its backups private and retain the external statement independently.

```sh
python3 services/epay_v2/reconciliation.py --database /private/epay/ledger.db \
  --pid 10001 --currency USD --from 2026-10-20T00:00:00Z --to 2026-10-21T00:00:00Z statement
python3 services/epay_v2/reconciliation.py --database /private/epay/ledger.db \
  --pid 10001 --currency USD --from 2026-10-20T00:00:00Z --to 2026-10-21T00:00:00Z \
  record --external-reference BANK-123 --evidence 'Matched against processor settlement BANK-123'
```

The RSA-only protocol remains unchanged: no MD5 fallback or downgrade is introduced.

The SQLite ledger serializes new refund admissions and counts successful, in-flight and unknown refunds against captured funds. Known native refunds created outside the gateway also count; native refund validation remains authoritative for direct native API activity. A short durable submission lease avoids simultaneous identical submissions. Definitive native failures may free refund capacity, but not the operation's ID. Transport errors, missing responses, unknown internal states, and native failed results without an error code keep capacity reserved. A background reader reconciles existing native refunds; it does not create new refunds. Refunds never restore the channel's daily collection allowance.

Protocol references: the [published PHP V2 SDK refund method](https://github.com/OpenAegis/epay_sdk_php_v2/blob/main/lib/EpayCore.class.php), and a deployed protocol publisher's [refund](https://zfqix.com/doc/pay_refund.html) / [refund query](https://pay.lanfucai.com/doc/pay_refundquery.html) documentation. These are interoperability references, not vendor certification; pending status `2` is documented here as an extension to the success/failure-only examples.

### Durability, callbacks and deployment limits

A persistent SQLite WAL ledger enforces `(pid, out_trade_no)` uniqueness, records order parameters and notification delivery state. The native payment ID is deterministic, so a crash during creation can recover by querying that same native payment before retrying. A repeated order with changed amount, type, description or callback is rejected. PID-scoped queries prevent cross-merchant order access.

The worker polls native stored payment status; configure native connector webhooks correctly. It does not turn browser returns or unverified caller messages into successful payments. Successful native payments enqueue signed GET notifications. Only a 2xx response containing `success` (surrounding whitespace allowed) acknowledges delivery. Attempts persist before sending; delivery is at least once, with up to ten attempts and exponential backoff capped at one hour. Merchants must deduplicate fulfillment by order ID. Back up the ledger: losing it loses compatibility order mapping and delivery state.

Callbacks/return URLs require HTTPS and public destinations. Callback connections pin the validated resolved IP, preserve TLS hostname verification, and never follow redirects. The loopback-only HTTP test override is `test_allow_loopback_callbacks`; do not enable it in production. Callbacks with private, link-local or reserved destinations are refused.

Run **one gateway instance per SQLite ledger**, on local persistent storage. This initial service does not claim multi-instance gateway HA; use a shared transactional database/outbox before horizontally scaling the compatibility service. Hyperswitch quota reservations themselves are PostgreSQL-backed and work across native router instances. Terminate HTTPS and apply normal request-rate limits at the reverse proxy.

## Verification

All tests/builds run on the designated remote debugging host, not by restoring local Redis/PostgreSQL/Node dependencies.

- `python3 scripts/test_channel_limits.py`: isolated PostgreSQL schema, real concurrent connections, shared pools, idempotency, amount/currency bypass, terminal states, policy changes and local-day buckets.
- `cd services/epay_v2 && python3 -m unittest -v test_gateway`: RSA, tampering, timestamps, HTTP parser, no MD5/V1, merchant boundaries, order conflicts and durable notification retries using a simulated backend.
- `python3 scripts/test_collection_payment_features.py`: private disposable fixtures, real native backend, platform/merchant authorization, zero-quota confirmed-payment rejection and both merchants' signed V2 hosted-link creation/query. It never submits a payment with a positive quota for actual charging.
- `python3 scripts/test_epay_refunds.py`: dedicated loopback router on 18081 with Stripe pointed to the loopback simulator on 18102. Creates fresh synthetic captured records in the disposable test database and drives RSA HTTP → native refund API → unchanged Stripe connector. Tests partial/refund concurrency, idempotency, ownership, native sync, declines, upstream 5xx and no restoration of channel quota. The test verifies the private debug configuration before starting and cannot use a real upstream.
- Existing Rust, collection-isolation and browser tests remain required.
