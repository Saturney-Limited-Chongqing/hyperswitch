# Rauthy dashboard login

Rauthy is an external OpenID Connect identity provider for the existing V1 login
flow of both the platform and
collection merchant dashboards. Payment connectors are unchanged. Existing Okta
configuration remains supported.

## Identity and authorization

Rauthy performs authentication. Hyperswitch continues to resolve active local
users, memberships, merchant context and roles from its own database. Rauthy roles
and groups never grant platform or merchant permissions.

Every Rauthy subject must be explicitly mapped to an existing local user ID in the
authentication method's encrypted private configuration. A matching email alone
never links an account. An absent or empty mapping denies login, including when a
local account with the same email exists. The verified ID-token email must belong
to that authentication method's configured email domain. No automatic signup is
implemented.

Bindings are scoped to the authentication method's configured issuer and client.
When moving to another Rauthy issuer, review the mappings rather than copying
subjects blindly. Updating mappings requires the Hyperswitch admin API key; never
put it or the OIDC client secret into frontend configuration.

## Configure Rauthy

Create a confidential OIDC client with:

- Authorization Code grant only, with S256 PKCE.
- `openid` and `email` scopes and an ID token containing `email_verified: true`.
- An exact redirect URI: `<dashboard-base-url>/redirect/oidc/rauthy`.
- RS256 ID-token signing, as used in the integration test.
- Client-enforced MFA for production deployments requiring MFA. The existing SSO
  flow skips Hyperswitch's local TOTP step.

Use the exact issuer from Rauthy's discovery document, normally
`https://<rauthy-host>/auth/v1/`, including its trailing slash. Set Hyperswitch
`[user].base_url` to the dashboard origin, not the API origin. Discovery, JWKS and
token endpoints must be reachable from the backend; the authorization endpoint
must also be reachable from the user's browser.

Complete any profile fields required by your Rauthy policy before testing login.
Rauthy HTTP 205 after password validation means that user-profile completion is
required; it is not a Hyperswitch role or token-validation failure. The remote
test uses the default optional preferred-username policy.

The remote test uses Rauthy `0.37.1` with an isolated persistent volume, loopback
HTTP and no SMTP. Production should use HTTPS and its own keys, users and client
configuration.

## Configure Hyperswitch

Create a private JSON connection file outside Git, with mode 0600:

```json
{
  "issuer": "https://identity.example.com/auth/v1/",
  "client_id": "hyperswitch",
  "client_secret": "<client-secret>",
  "owner_id": "<tenant-or-organization-id>",
  "owner_type": "tenant",
  "email_domain": "example.com",
  "subject_bindings": {
    "<rauthy-platform-user-sub>": "<existing-local-platform-user-id>",
    "<rauthy-merchant-user-sub>": "<existing-local-merchant-user-id>"
  }
}
```

Use `owner_type: "organization"` for an organization-owned authentication method.
The owner must match the intended deployment. The configuration script creates an
authentication method with `allow_signup: false`:

```sh
export HYPERSWITCH_API_URL=https://api.example.com
export HYPERSWITCH_ADMIN_KEY='<admin-api-key>'
python3 -B scripts/configure_rauthy_sso.py /private/path/connection.json
```

It prints only the method `id` and `auth_id`. Update an existing method explicitly:

```sh
python3 -B scripts/configure_rauthy_sso.py /private/path/connection.json \
  --update-id '<method-id>'
```

Open `<dashboard-base-url>/?auth_id=<auth_id>` to select this login configuration.
The page displays **Continue with Rauthy**. Several identities may use one client
while retaining separate local permissions. Existing password methods are not
created or deleted by the script; choose that policy explicitly for the domain.

The admin API represents this configuration as an `auth_method` object containing
`auth_type: "open_id_connect"`, `public_config: {"name": "rauthy"}` and
`private_config` with `base_url`, `client_id`, `client_secret`, optional
`private_key`, and `subject_bindings`.

## Session and security behavior

The authorization request sets an HttpOnly, SameSite=Lax state cookie scoped to
the initiating browser. HTTPS deployments use a Secure `__Host-` cookie. A valid
callback from a different browser is rejected before exchanging its code.

The backend requests S256 PKCE, verifies the ID token through the existing OIDC
library and nonce, requires verified email for Rauthy, and consumes the successful
callback's nonce and PKCE verifier. Reusing the callback cannot create another
session. Unconfigured, expired or unknown state fails closed.

Successful login creates the normal Hyperswitch dashboard JWT. Rauthy logout,
account suspension and client-secret changes do not themselves revoke an already
issued Hyperswitch JWT. This integration does not implement refresh-token storage,
SCIM provisioning, or OIDC backchannel/single logout. Use Hyperswitch session
revocation for immediate local session termination.

## Remote verification

Run builds and tests on `flan@10.100.100.22`. The dedicated Rauthy configuration,
bootstrap files and private fixtures live under
`/home/flan/Projects/hyperswitch-debug/rauthy/`; the container is
`hyperswitch-debug-rauthy`. No new local build dependencies are required.

Browser tests use existing project dependencies and the remote Chrome installation:

```sh
export COLLECTION_TEST_FIXTURES=/home/flan/Projects/hyperswitch-debug/fixtures.json
export RAUTHY_TEST_FIXTURES=/home/flan/Projects/hyperswitch-debug/rauthy/browser-fixtures.json
cd /home/flan/Projects/hyperswitch-control-center
npx playwright test --config playwright.remote.local.config.ts --grep Rauthy
```

The machine-specific Playwright config stays outside version control. The tests
exercise the actual login button, Rauthy login, S256 authorization request, callback,
local role resolution, merchant management restrictions, same-email unbound-account
rejection, unknown state, cross-browser callback rejection and callback replay. They do not mock the identity provider
or inject a dashboard JWT for the Rauthy login scenarios.
