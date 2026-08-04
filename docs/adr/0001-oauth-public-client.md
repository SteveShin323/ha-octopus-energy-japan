# ADR 0001: Use an OAuth public client

Status: accepted, pending OEJP application approval
Date: 2026-07-29

## Context

OEJP has deprecated email/password fields in `ObtainJSONWebTokenInput` and
recommends its authorization server. A Home Assistant custom integration cannot
keep a distributed client secret confidential.

## Decision

Use Authorization Code with PKCE S256 as the primary authentication method.
Support Device Authorization Grant through the same `AuthSession` boundary when
OEJP enables it. Never request or store the customer's OEJP password in the
public integration. Never distribute a client secret.

The client ID is committed only if OEJP authorizes a shared public client.
Otherwise, use Home Assistant Application Credentials. The legacy Kraken token
operation is isolated to a local read-only probe and removed from public setup
and runtime before alpha.

## Consequences

- OAuth application approval is a release blocker.
- Tokens remain local to each Home Assistant instance.
- Revocation and refresh failures use Home Assistant reauthentication.
- Tests need a deterministic local OAuth server and fake auth session.
- The authorization scheme and token rotation behavior remain configuration
  facts until OEJP confirms them.

## Provider evidence, 2026-08-04

The provider's published OpenID Connect discovery document confirms the
authorization, token, revocation, userinfo, and JWKS endpoints, the `code` response
type, and every scope this integration requests. Those are recorded in
`oauth_metadata.py`. The `Bearer` header scheme was confirmed against the live
GraphQL API.

Two parts of this decision are **not** yet supported by that document:

- `token_endpoint_auth_methods_supported` lists only `client_secret_post` and
  `client_secret_basic`, not `none`, so public-client token exchange is
  unconfirmed; and
- no `code_challenge_methods_supported` entry is published, so PKCE is
  unadvertised.

Neither is proof of absence, and no other approach is open to a HACS integration,
so the decision stands. Both are now explicit questions in
[`OAUTH_APPLICATION_STATUS.md`](../OAUTH_APPLICATION_STATUS.md). If a client secret
turns out to be mandatory, this ADR must be superseded rather than worked around,
because a distributed secret is not a secret.
