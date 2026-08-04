# OEJP OAuth application status

Status: endpoints and scopes confirmed from the provider's published discovery
document; a client ID and written public-client approval are still pending
Last updated: 2026-08-04

## Implementation status

The public Home Assistant email/password config flow has been removed.
Authorization Code + PKCE, refresh-token rotation, one-time authentication
retry, reauthentication, best-effort revocation, and RFC 8628 device grant
transport are implemented.

The provider's published endpoints, issuer, scopes, and the confirmed `Bearer`
header scheme are now recorded in `oauth_metadata.py`, so Application Credentials
constructs a PKCE public client without waiting for a reply. Nothing is substituted
from another Kraken territory, and the module still fails closed if that metadata
is ever removed.

Device authorization remains implemented only at the transport and session
boundary. It is not exposed in the setup UI because the discovery document
advertises no device-authorization endpoint.

## Requested application

The project maintainer requested a read-only public OAuth application from
Octopus Energy Japan for this repository.

Requested primary grant:

- Authorization Code Grant;
- PKCE S256;
- redirect URI `https://my.home-assistant.io/redirect/oauth`; and
- no client secret in Home Assistant or the public repository.

Requested additional grant:

- Device Authorization Grant, preferably on the same application.

The request describes 30-minute consumption polling, slower discovery,
contract/tariff/billing cadences, daily reconciliation, exponential backoff,
local-only tokens and statistics, no external telemetry, and redaction of
customer identifiers.

## Acknowledgement

OEJP has acknowledged the request and reported that the responsible team is
handling it. No application, client ID, endpoint, scope, or permission decision
has been received, so every row of the response record below remains pending and
the implementation continues to fail closed.

Until the application arrives, live contract investigation uses the isolated
local probe with the deprecated Kraken email/password login, as described in
[`FIXTURE_REDACTION.md`](FIXTURE_REDACTION.md). That path exists only in
`scripts/oejp_probe.py`. The Home Assistant config flow and runtime cannot use
it, and no release depends on it.

## Release blocker

No OAuth client ID exists, so no user can complete the setup flow. That is now the
only thing standing between this integration and a working release, together with
written confirmation that the application may be registered as a public client.

A user cannot work around it by supplying their own client ID through Application
Credentials either, because OEJP does not offer self-service application
registration.

## Response record

The provider publishes an OpenID Connect discovery document at
`https://auth.oejp-kraken.energy/.well-known/openid-configuration`. It was read on
2026-08-04 and settles most of this table without waiting for a reply. Nothing
below is inferred from another Kraken territory.

| Item | Confirmed value | Source |
|---|---|---|
| Authorization endpoint | `https://auth.oejp-kraken.energy/authorize/` | discovery document |
| Token endpoint | `https://auth.oejp-kraken.energy/token/` | discovery document |
| Issuer | `https://auth.oejp-kraken.energy/token/` | discovery document, verbatim |
| Revocation endpoint | `https://auth.oejp-kraken.energy/revoke-token/` | discovery document |
| UserInfo endpoint | `https://auth.oejp-kraken.energy/userinfo/` | discovery document |
| JWKS URI | `https://auth.oejp-kraken.energy/.well-known/jwks.json` | discovery document |
| Authorization Code response type | supported (`code`) | discovery document |
| Required OAuth scopes | all fourteen requested scopes are advertised | discovery document |
| Available claims | `sub` only | discovery document |
| GraphQL Authorization header scheme | `Bearer` | live API: `Bearer`, `JWT`, and a bare token were all accepted; a missing header returned `KT-CT-1112` |
| Device authorization endpoint | **not advertised** | discovery document |
| Shared public client ID may be published | Pending | requires a written reply |
| Public client without a secret | **contradicted, see below** | discovery document |
| PKCE enabled | **not advertised, see below** | discovery document |
| Same or separate client ID per grant | Pending | requires a written reply |
| Registered redirect URI | Pending | requires the issued application |
| Access-token lifetime | Pending | requires the issued application |
| Refresh-token lifetime | Pending | requires the issued application |
| Refresh-token rotation behavior | Pending | requires the issued application |
| Generic readings access under OAuth | Pending | legacy-login access is not evidence |
| Legacy readings access under OAuth | Pending | legacy-login access is not evidence |
| Billing and official-cost access under OAuth | Pending | `marketSupplyAgreements` is already forbidden to the legacy account user |

## Two findings that challenge ADR 0001

[ADR 0001](adr/0001-oauth-public-client.md) assumes a public client using
Authorization Code with PKCE and no client secret. The discovery document does not
support that assumption yet:

1. `token_endpoint_auth_methods_supported` lists only `client_secret_post` and
   `client_secret_basic`. It does **not** list `none`, which is what a public
   client needs to call the token endpoint without a secret.
2. There is no `code_challenge_methods_supported` entry, so PKCE is not
   advertised. Many servers support PKCE without advertising it, so this is not
   proof of absence, but it is not confirmation either.

`id_token_signing_alg_values_supported` offers `HS256`, which a public client could
not verify because it holds no shared secret. That does **not** need to be asked:
the integration never parses or verifies an ID token. Login identity comes from the
API with `viewer { id }`, so the signing algorithm is irrelevant to it. See
[ADR 0002](adr/0002-login-scoped-config-entry.md).

These must be asked explicitly, because a mandatory client secret cannot be
satisfied by a HACS integration:

- can the application be registered as a public client, so the token endpoint
  accepts `none` for client authentication;
- is PKCE `S256` accepted on the authorization and token endpoints even though it
  is not advertised; and
- is the Device Authorization Grant available at all, given no device endpoint is
  advertised.

Until the first two are answered in writing, the integration ships the published
endpoints and requests PKCE, but a user still cannot connect because no client ID
exists.

## Decision rules

- If a shared public client is approved, its non-secret client ID may be included
  after written confirmation.
- If user-specific credentials are required, use Home Assistant Application
  Credentials.
- If a client secret is mandatory, do not distribute it and renegotiate public
  client support.
- If OAuth is rejected, continue fixture-based engineering but do not publish a
  functional integration release.
