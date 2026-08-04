# OEJP OAuth application status

Status: endpoints confirmed and all fourteen scopes advertised in the provider's
published discovery document, and public client, PKCE, and the device grant all
offered in the provider's own documentation; a client ID, the scopes actually
granted to this application, and written public-client approval are still pending
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
boundary, and is not exposed in the setup UI. The reason is no longer that the
endpoint is missing: the provider documents `/device-authorization/` and the live
endpoint answers, so the URL is now recorded in `oauth_metadata.py`. It is not
exposed because no client ID exists for any grant, and because the setup path
should be chosen once, on evidence, rather than offering two unusable options.

Device authorization is the better path for this integration once a client ID
exists: it needs no redirect URI at all, which removes the My Home Assistant
requirement described below.

## Submitted application request

A read-only public OAuth application has been requested from Octopus Energy Japan
for this repository. The request has been sent; what follows is what it contains,
so that a reply can be checked against it.

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
customer identifiers. It enumerates the read capabilities the integration needs
in prose and asks OEJP to attach scopes and GraphQL permissions accordingly.

It asks three questions explicitly:

1. may the issued public client ID be published in this repository and shared
   across many Home Assistant installations;
2. are both an access token and a refresh token issued under Authorization Code
   with PKCE, and what is the refresh token's lifetime and rotation behavior; and
3. is `Bearer` the correct Authorization header scheme for an OAuth access token
   on the GraphQL API.

Question 3 is not redundant with the observation recorded below. That observation
used a legacy email/password token; no OAuth access token has ever been sent to
this API, because none exists yet.

Question 1 also carries the public-client question without naming it. A client ID
cannot be both publishable to everyone and paired with a mandatory secret, so a
plain "yes" settles public-client support even if the reply never mentions
`none`. A reply that grants publication *and* issues a secret is
self-contradictory and must be treated as unanswered.

### One redirect URI serves every installation

Only the shared `https://my.home-assistant.io/redirect/oauth` was submitted. That
address is operated by the Home Assistant project and forwards the authorization
response to whichever local instance began the flow, which is how one public
client can serve installations that have no public address of their own.

This was not explained in the submitted request. If OEJP questions why a
third-party domain is the redirect target, that is the explanation to send, not a
change of approach: registering per-installation URLs is not possible for software
distributed to arbitrary private networks.

The integration now refuses to start sign-in when the `my` integration is not
loaded, because Home Assistant would otherwise build this instance's own callback
URL, which OEJP has not registered.

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

Two provider sources settle most of this table without waiting for a reply, and
nothing below is inferred from another Kraken territory:

- the OpenID Connect **discovery document** at
  `https://auth.oejp-kraken.energy/.well-known/openid-configuration`, read on
  2026-08-04; and
- the provider's own **auth-server documentation** at
  `https://auth.oejp-kraken.energy`, read the same day, which documents four grant
  types (authorization with PKCE, client credentials, device code, token exchange)
  and asks an applicant to state a client type of "public or confidential".

Where they disagree, the documentation plus a live probe wins over the discovery
document, because the discovery document is demonstrably incomplete: it omits the
device-authorization endpoint that both the documentation and the live server have.

| Item | Confirmed value | Source |
|---|---|---|
| Authorization endpoint | `https://auth.oejp-kraken.energy/authorize/` | discovery document |
| Token endpoint | `https://auth.oejp-kraken.energy/token/` | discovery document |
| Issuer | `https://auth.oejp-kraken.energy/token/` | discovery document, verbatim |
| Revocation endpoint | `https://auth.oejp-kraken.energy/revoke-token/` | discovery document |
| UserInfo endpoint | `https://auth.oejp-kraken.energy/userinfo/` | discovery document |
| JWKS URI | `https://auth.oejp-kraken.energy/.well-known/jwks.json` | discovery document |
| Authorization Code response type | supported (`code`) | discovery document |
| Required OAuth scopes advertised by the server | all fourteen appear in `scopes_supported` | discovery document |
| ID token signing algorithms | `HS256` and `RS256` | discovery document; irrelevant here, no ID token is parsed |
| Available claims | `sub` only | discovery document |
| Required OAuth scopes granted to this application | Pending | advertised is not granted, see below |
| GraphQL Authorization header scheme | `Bearer` accepted with a legacy token | live API: `Bearer`, `JWT`, and a bare token were all accepted; a missing header returned `KT-CT-1112`. Never tested with an OAuth access token. The provider's documentation shows a bare token |
| Device authorization endpoint | `https://auth.oejp-kraken.energy/device-authorization/` | provider documentation; live POST answered `invalid_request: Invalid client_id parameter value`, so the endpoint exists. **Absent from the discovery document** |
| Device Authorization Grant offered | yes, as one of four documented grant types | provider documentation |
| Shared public client ID may be published | Pending | asked; requires a written reply |
| Public client offered as a client type | yes, applicants choose "public or confidential" | provider documentation. `none` is still absent from `token_endpoint_auth_methods_supported`, see below |
| PKCE offered | yes, "Authorization with PKCE" is a documented grant | provider documentation. No `code_challenge_methods_supported` entry, see below |
| API key authentication | **not documented for customers** | the shared Kraken schema has `APIKey` input, a `view:api-key` scope, `AccountUser.liveSecretKey` (null on a real account), and `regenerateSecretKey` whose documented errors include `KT-CT-11100: API key authentication unavailable`. OEJP's own documentation defers all authentication to the auth server and never mentions a key. Not a supported path |
| Same or separate client ID per grant | Pending | asked; requires a written reply |
| Registered redirect URI | Pending | requested as `https://my.home-assistant.io/redirect/oauth` |
| Access-token lifetime | Pending | requires the issued application |
| Refresh-token lifetime | Pending | requires the issued application |
| Refresh-token rotation behavior | Pending | requires the issued application |
| Generic readings access under OAuth | Pending | legacy-login access is not evidence |
| Legacy readings access under OAuth | Pending | legacy-login access is not evidence |
| Billing and official-cost access under OAuth | Pending | `marketSupplyAgreements` is already forbidden to the legacy account user |

## Two gaps in the discovery document

[ADR 0001](adr/0001-oauth-public-client.md) assumes a public client using
Authorization Code with PKCE and no client secret. Two entries the discovery
document does not contain would confirm that outright:

1. `token_endpoint_auth_methods_supported` lists only `client_secret_post` and
   `client_secret_basic`. It does **not** list `none`, which is what a public
   client needs to call the token endpoint without a secret.
2. There is no `code_challenge_methods_supported` entry, so PKCE is not advertised.

Both read as gaps rather than refusals, for a reason stronger than "many servers
support PKCE without advertising it". The provider's own documentation asks an
applicant to choose a client type of **"public or confidential"** and lists
**"Authorization with PKCE"** among its grant types. OEJP therefore offers exactly
what this project requested, in its own words, and the discovery document is
independently proven incomplete: it omits a device-authorization endpoint that the
same documentation describes and the live server answers.

`token_endpoint_auth_methods_supported` is also a property of the server as a whole,
while client type is set per application at registration. A server that registers
public clients need not advertise `none` globally.

`id_token_signing_alg_values_supported` offers `HS256` and `RS256`. A public client
could not verify `HS256`, holding no shared secret. That does **not** need to be
asked: the integration never parses or verifies an ID token. Login identity comes
from the API with `viewer { id }`, so the signing algorithm is irrelevant to it. See
[ADR 0002](adr/0002-login-scoped-config-entry.md).

Both were submitted with the application request, as required specification rather
than as questions in their own right: it asks for a public client with no secret in
Home Assistant or the repository, and for PKCE S256. Neither still needs asking.
Both still need **confirming for this application in the reply**: what the provider
offers in general is not yet what it has granted here, and a mandatory client secret
cannot be satisfied by a HACS integration.

What to check when a reply arrives, in order:

1. **Is a client secret issued or required?** If yes, the reply contradicts
   question 1 about publication and is not an answer; see the decision rules.
2. **Do the granted scopes match `READ_ONLY_SCOPES` in `oauth_metadata.py`,
   string for string?** The submitted request describes capabilities in prose,
   while the integration sends fourteen exact scope strings in the authorize
   request. An application provisioned with different names, a coarser grant, or
   one scope omitted fails with `invalid_scope` before the user ever sees a
   consent screen, and nothing in the repository would explain why. Reconcile the
   granted set against that constant before the first connection attempt.
3. **Is PKCE `S256` accepted** on the authorization and token endpoints, even
   though it is unadvertised in the discovery document.
4. **Was the Device Authorization Grant enabled on this application?** The endpoint
   exists and the grant is documented, so this is about provisioning, not
   availability. It is worth having: device flow needs no redirect URI, so it would
   remove the My Home Assistant requirement entirely and make it the better setup
   path of the two.

Until 1 and 3 are settled, the integration ships the published endpoints and
requests PKCE, but a user still cannot connect because no client ID exists.

## Decision rules

- If a shared public client is approved, its non-secret client ID may be included
  after written confirmation.
- If user-specific credentials are required, use Home Assistant Application
  Credentials.
- If a client secret is mandatory, do not distribute it and renegotiate public
  client support.
- If OAuth is rejected, continue fixture-based engineering but do not publish a
  functional integration release.
