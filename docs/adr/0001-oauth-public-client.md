# ADR 0001: Use an OAuth public client

Status: accepted; implemented and kept, but not reachable — the provider will issue no client
Date: 2026-07-29
Amended by: [ADR 0008](0008-password-authentication.md), on the single point of
whether the email and password login may be offered to users

## Context

OEJP has deprecated email/password fields in `ObtainJSONWebTokenInput` and
recommends its authorization server. A Home Assistant custom integration cannot
keep a distributed client secret confidential.

## Decision

Use Authorization Code with PKCE S256 as the primary authentication method.
Support Device Authorization Grant as a selectable setup method through the same
`AuthSession` boundary: it uses the same client ID and the same token endpoint, so its
tokens refresh identically, and it needs no redirect URI at all. Never distribute a client
secret.

~~Never request or store the customer's OEJP password in the public integration.~~
Superseded by [ADR 0008](0008-password-authentication.md), which permits it as one
selectable method for as long as no client ID exists.

The client ID is committed only if OEJP authorizes a shared public client.
Otherwise, use Home Assistant Application Credentials. The legacy Kraken token
operation is isolated to a local read-only probe and removed from public setup
and runtime before alpha.

**Where the client ID lives, decided 2026-08-06.** It ships in the code as
`oauth_metadata.OEJP_OAUTH_CLIENT_ID`, empty until issued, and is registered as an OAuth
implementation. It identifies the application, not the customer, so it is the same for
everyone and asking each user to type it is a setup step that can only be got wrong — and
Application Credentials additionally requires a client *secret*, which a public client does
not have. Home Assistant omits an empty secret from the token request, so nothing is sent.

Registration happens in two places, and both are needed. `async_setup` covers loading an
existing entry after a restart. The config flow registers it again, because a
config-entry-only integration with no entries is never set up: measured against a real
instance, a flow started there found the integration absent from `hass.config.components`
and aborted for want of a client.

Application Credentials stays as the override. The discovery document advertises neither a
`none` token-endpoint auth method nor `code_challenge_methods_supported`, so a public client
is what the provider's documentation describes rather than what its metadata proves. If the
issued client turns out to be confidential, adding a credential by hand is the way through,
and a hand-added credential is offered alongside the shipped one.

> [!NOTE]
> That last sentence no longer holds. [ADR 0008](0008-password-authentication.md)
> amends it, because no OAuth application arrived and the legacy login became the only
> way for anyone to connect. The rest of this decision stands unchanged.

> [!IMPORTANT]
> **Octopus Energy Japan will not issue one.** Asked directly, its customer service
> replied on 2026-08-06 that the capability is not supported in Japan and that it does
> not offer an API service to individual customers. There is no client to wait for.
>
> Nothing here is withdrawn. The implementation is complete and measured against the
> provider's own authorization server, and it costs nothing to keep: the client id is an
> empty constant, and filling it in is the only change needed. What changed is that the
> two OAuth methods are no longer offered in the setup menu while no client exists —
> a choice that can only end in an apology is worse than no choice. They reappear by
> themselves if a client is ever issued, or if a user adds one of their own.

## Consequences

- ~~OAuth application approval is a release blocker.~~ It is not: the provider has said
  it will not issue one, so waiting would block the release forever. Email and password
  is the supported path, under [ADR 0008](0008-password-authentication.md).
- Tokens remain local to each Home Assistant instance.
- Revocation and refresh failures use Home Assistant reauthentication.
- Tests need a deterministic local OAuth server and fake auth session.
- The authorization scheme and token rotation behavior remain configuration
  facts until OEJP confirms them.

## Provider evidence, 2026-08-04

The provider's published OpenID Connect discovery document confirms the
authorization, token, revocation, userinfo, and JWKS endpoints, the `code` response
type, and every scope this integration requests. The device-authorization endpoint
is absent from it but documented by the provider and confirmed live. Those are
recorded in `oauth_metadata.py`. The `Bearer` header scheme was confirmed against
the live GraphQL API with a legacy token.

Two parts of this decision are **not** confirmed by that document:

- `token_endpoint_auth_methods_supported` lists only `client_secret_post` and
  `client_secret_basic`, not `none`, so public-client token exchange is
  unconfirmed; and
- no `code_challenge_methods_supported` entry is published, so PKCE is
  unadvertised.

The provider's own auth-server documentation, read the same day, supports both:
it asks an applicant to state a client type of "public or confidential" and lists
"Authorization with PKCE" among its grant types. The discovery document is also
proven incomplete, because it omits the device-authorization endpoint that the same
documentation describes and the live server answers. Both entries therefore read as
metadata gaps rather than refusals, and no other approach is open to a HACS
integration, so the decision stands. If a client secret turns out to be mandatory,
this ADR must be superseded rather than worked around, because a distributed secret is
not a secret.

## Redirect URI consequence, 2026-08-04

A single public client can only be registered against a single redirect URI, and
`https://my.home-assistant.io/redirect/oauth` is the one Home Assistant provides
for exactly this purpose. Home Assistant chooses it only when the `my` integration
is loaded; otherwise it builds this instance's own callback URL, which OEJP will
not have registered.

The config flow therefore aborts with a translated message when `my` is absent,
rather than letting the user reach the provider's unregistered-redirect error
part-way through sign-in.
