# Platform and collection merchant dashboards

The business dashboard is maintained in the separate
[Hyperswitch Control Center repository](https://github.com/juspay/hyperswitch-control-center).
This repository runs its container on port 9000 in `docker-compose.yml` and
`docker-compose-development.yml`. The files under `cypress-tests/dashboard` are
test reports, not the business dashboard.

## Backend access model

A collection tenant is a Merchant. Its Business Profiles remain within that
Merchant. Infrastructure multitenancy (`multitenancy.tenants`) is a separate
deployment boundary; creating a collection merchant does not require another
database schema.

The existing tenant administrator and administrative service credentials retain
their existing management capabilities. They are trusted platform credentials,
not credentials to distribute to collection merchants. Existing route and tenant
checks still apply; this change does not introduce a cross-tenant administrator
bypass.

Two predefined merchant roles support the collection dashboard:

| Role | Capabilities |
| --- | --- |
| `collection_merchant_admin` | Merchant-scoped payment operations, refunds, customers, disputes, reports, analytics, members, API keys, and webhook events |
| `collection_merchant_view_only` | Merchant-scoped operation and analytics reads |

Neither role can view or manage connectors, routing, connector cloning, payouts,
organization administration, or tenant administration. Account writes are denied
even though the legacy OperationsManage group includes the Account resource.
The implicit ConnectorsView dependency of Operations groups is removed for these
roles. Resource discovery uses the same filtered role resource set.

Collection administrators can invite or assign only these two collection roles.
They cannot create or edit custom roles, or change/delete platform-management
members. This avoids escalating to the legacy `merchant_admin`, which is at the
same entity level but has channel-management permissions.

## Provisioning

Platform operators must do both of the following before distributing credentials:

1. Provision membership at Merchant scope using a collection role. Do not grant
   organization/tenant roles to collection customers. Existing broader memberships
   must be removed and their sessions invalidated through the existing mechanisms.
2. Register the Merchant ID under the existing `[platform]` settings:

   ```toml
   [platform]
   # Preserve the deployment's existing enabled / allow_connected_merchants values.
   collection_merchant_ids = ["merchant_a", "merchant_b"]
   ```

The registry restricts those merchants' service API keys to an exact allowlist of
collection flows in `services/authorization/collection.rs`. The check runs after
authentication, before executing the handler in the shared API wrapper. Merchant
IDs come from authenticated credentials. Requests for unlisted or future flows are
denied; platform admin credentials are unaffected. Registered merchants also cannot
supply request-level channel credentials or routing/connector overrides. Ordinary
metadata is not interpreted as a channel override. Payments and refunds continue
using the existing connector architecture.

The registry defaults to empty for compatibility with existing orchestration
deployments. Assigning a dashboard role alone does **not** restrict a service key;
both provisioning steps are required. Registry changes require configuration
reload/restart according to the deployment's existing process.

## Control Center integration

The requested frontend fork is cloned independently into `control-center/`:
https://github.com/Saturney-Limited-Chongqing/hyperswitch-control-center

The pinned base revision is in `config/control_center_collection_revision`.
The reviewed source changes are also exported in
`patches/control-center-collection.patch`, so the ignored nested checkout is not
required to recover the implementation:

```sh
bash scripts/setup_collection_control_center.sh
cd control-center
npm ci
npm run re:build
npm run test:dashboard-access
```

Platform administrators (`tenant_admin` and `internal_admin`) receive a merchant
management hub at `/dashboard/platform`. It uses the existing authenticated
merchant-switching APIs; channel configuration and routing remain within the
selected merchant context. Collection roles receive a dedicated collections home
and navigation. Both sidebar feature-flag variants use that navigation, and the
application rejects direct visits to management pages for collection roles.

The dashboard waits for current merchant data and permissions before displaying
content. Pending responses from a previous session are ignored. Merchant/profile
changes remount page content and hide the previous session immediately.

For local integration, the frontend supports `PORT` and `API_PROXY_TARGET`:

```sh
PORT=19000 API_PROXY_TARGET=http://127.0.0.1:18080 \
  default__endpoints__api_url=http://127.0.0.1:19000/api npm start
```

Webhook event viewing/retry uses existing dedicated permissions. Editing webhook
destinations still requires a dedicated API because broad AccountWrite is
unavailable to collection roles. Shared channel ownership, automated merchant
provisioning, fee accounting, and settlement are separate work.

## Verified behavior and reproduction

The local verification used PostgreSQL 17 on port 15432, Redis on port 16379, the
backend on port 18080, and Control Center on port 19000. Existing MockDb library
tests additionally require Redis on port 6379. Protobuf supplies `protoc`.
A debug backend requires an increased worker-thread stack for connector creation:

```sh
RUST_MIN_STACK=33554432 target/debug/router -f /path/to/test/router.toml
```

The disposable backend config must include both `collection_a` and `collection_b`
in `platform.collection_merchant_ids` and point all database/Redis settings at the
test instances. Apply the V1 migrations before provisioning fixtures. The fixture
script provisions one organization, two merchants, a platform administrator,
collection administrators, and two stored Stripe test configurations. It does not
confirm payments against Stripe.

```sh
export COLLECTION_TEST_FIXTURES=/private/tmp/collection-test-fixtures.json
python3 -B scripts/bootstrap_collection_test.py --provision-disposable-database
python3 -B scripts/test_collection_isolation.py
cd control-center
npx playwright test --config playwright.collection.config.ts
```

Fixture files contain test credentials, are created with mode 0600, and must stay
outside version control. Keep the same fixture environment variable when running
HTTP and browser checks.

Verified results:

| Check | Result |
| --- | --- |
| Backend library suite (V1, OLAP, OLTP, redis-rs) | 108 passed |
| HTTP isolation checks against the running backend | 34 passed |
| Frontend route policy tests | 5 passed |
| Chromium integration scenarios | 2 passed |
| ReScript build and webpack development build | Passed |

The HTTP checks cover same-ID customer isolation, cross-merchant order/customer
reads, membership switching, cross-merchant API-key creation, channel access,
request-level routing overrides, custom roles, and invitation escalation. The
browser checks inspect both merchants' actual customer lists, reject direct
management URLs, and switch the platform administrator to merchant B to verify
that only B's channel appears.

The full backend suite uses rollback-only stores. Their replica readers now share
writer transactions, so tests see their own writes; normal database pools retain
the existing behavior. Payment-attempt fixtures now create their encryption keys
and use the matching processor merchant. The webhook concurrency test uses a
unique object ID and propagates task errors.

To run that suite, set `CONFIG_DIR` to the directory containing the disposable
`development.toml` and use an absolute fallback seed path (Cargo runs library tests
from the crate directory):

```sh
CONFIG_DIR=/path/to/test/config \
ROUTER__SUPERPOSITION__BACKUP_FILE_PATH="$PWD/config/superposition_seed.toml" \
RUST_MIN_STACK=33554432 \
cargo test -p router --lib --no-default-features \
  --features v1,olap,oltp,redis-rs -- --test-threads=1
```

Payment verification in this run covers unconfirmed order creation and ownership.
Live processor authorization, settlement, and end-to-end webhook delivery were not
exercised.
