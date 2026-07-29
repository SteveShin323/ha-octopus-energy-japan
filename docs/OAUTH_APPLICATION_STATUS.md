# OEJP OAuth application status

Status: waiting for OEJP response
Last updated: 2026-07-29

## Implementation status

The public Home Assistant email/password config flow has been removed.
Authorization Code + PKCE, refresh-token rotation, one-time authentication
retry, reauthentication, best-effort revocation, and RFC 8628 device grant
transport are implemented behind provider-confirmed metadata.

The implementation fails closed while the response table below is incomplete:
it does not substitute endpoints or header schemes from another Kraken
territory. Application Credentials can construct a PKCE public client as soon
as the confirmed metadata is recorded. Device authorization is implemented at
the transport/session boundary; it will be exposed in the Home Assistant setup
UI only if OEJP confirms the grant and endpoint.

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

## Release blocker

No OAuth client ID is currently committed. No public functional release will be
made until OEJP confirms the allowed application and permission model.

## Response record

Complete this section from the authoritative OEJP reply before implementing
production OAuth:

| Item | Confirmed value |
|---|---|
| Shared public client ID may be published | Pending |
| Authorization Code + PKCE enabled | Pending |
| Device Authorization Grant enabled | Pending |
| Same or separate client ID per grant | Pending |
| Exact authorization endpoint | Pending |
| Exact token endpoint | Pending |
| Exact device authorization endpoint | Pending |
| Registered redirect URI | Pending |
| Required OAuth scopes | Pending |
| GraphQL permissions | Pending |
| Access-token lifetime | Pending |
| Refresh-token lifetime | Pending |
| Refresh-token rotation behavior | Pending |
| GraphQL Authorization header scheme | Pending |
| Generic readings access | Pending |
| Legacy readings access | Pending |
| Billing and official-cost access | Pending |
| Revocation endpoint and behavior | Pending |

## Decision rules

- If a shared public client is approved, its non-secret client ID may be included
  after written confirmation.
- If user-specific credentials are required, use Home Assistant Application
  Credentials.
- If a client secret is mandatory, do not distribute it and renegotiate public
  client support.
- If OAuth is rejected, continue fixture-based engineering but do not publish a
  functional integration release.
