# ADR 0008: Offer the provider's email and password login, and store the credential

Status: accepted
Date: 2026-08-04
Amends: [ADR 0001](0001-oauth-public-client.md)

## Context

[ADR 0001](0001-oauth-public-client.md) decided that the integration would never
request or store the customer's OEJP password, and removed the email/password login
from public setup and runtime. That was the right call for a project that expected an
OAuth application to arrive.

It has not arrived, and there is no self-service way to create one, so **no user can
connect by any means**. Every path was measured against a real account on 2026-08-04:

| Path | Result |
|---|---|
| Authorization Code + PKCE | needs a client ID that does not exist |
| Device Authorization Grant | endpoint is live and documented, still needs a client ID |
| API key (`APIKey` input, `liveSecretKey`, `regenerateSecretKey`) | not documented for customers; `liveSecretKey` is null and `KT-CT-11100: API key authentication unavailable` is a documented error |
| Long-lived refresh token | the provider's own description limits `obtainLongLivedRefreshToken` to "authorized third-party organizations only" and states that "account users can only generate short-lived refresh tokens" |
| Short-lived refresh token alone | access token 1 hour, refresh token **7 days**, renewal does **not** rotate it and does **not** extend its expiry |

The last row is what forces the decision. A stored refresh token buys at most seven
days from one sign-in. After that only the credential itself can produce a token, so
a refresh-token-only design would ask the user to sign in again every week — which is
not an unattended integration.

## Decision

Offer email and password as a **selectable login method** alongside OAuth, and store
the email and password in the config entry so the integration can sign in again when
the refresh token expires.

This amends ADR 0001 on one point only. Everything else in ADR 0001 stands: OAuth
remains the primary and recommended method, no client secret is ever distributed, and
the OAuth methods still never see the password.

Specifically:

- the setup flow opens with a method menu, OAuth listed first and marked recommended;
- the password method needs no application credential and no redirect URI, so it does
  not require My Home Assistant;
- the credential lives in `entry.data`, which is plain text on disk. It is never
  logged, never placed in state or attributes, and never included in diagnostics;
- one OEJP login owns one config entry regardless of method, because the identity is
  `HMAC(installation secret, OEJP_AUTH_ISSUER + viewer.id)` and the issuer term is a
  constant rather than the method. Reauthentication returns to the method menu, so a
  password entry can be **promoted to OAuth in place**, keeping its stored readings
  and Energy Dashboard history; and
- promotion replaces the entry data rather than merging it, so the stored password is
  deleted when the entry moves to OAuth, and a stale OAuth token is deleted when it
  moves the other way.

## Failure handling

The provider reports a rejected credential as `VALIDATION/KT-CT-1138`, which the
error classifier maps to `OejpAuthenticationError` — the same class an expired access
token produces. The two must not be conflated, because one is recoverable by renewing
and the other is not recoverable at all.

So `OejpPasswordAuthSession` raises its own `OejpPasswordCredentialRejected` when a
**full sign-in** is rejected, and setup turns that into `ConfigEntryAuthFailed`. A
rejected renewal falls back to a full sign-in; a rejected sign-in never retries.
Without that split, a changed password would loop indefinitely against a live
endpoint.

## Consequences

- A user can connect today, for the first time in this project's life.
- The privacy statement becomes conditional on the method chosen, which
  [`PRIVACY.md`](../../PRIVACY.md) now states explicitly.
- This method will stop working. `email` and `password` are no longer among the
  introspected fields of `ObtainJSONWebTokenInput` — only `APIKey`,
  `organizationSecretKey`, `preSignedKey`, `refreshToken`, and `captchaResponse` are —
  yet the provider still honours them. A field that is hidden but honoured can stop
  being honoured without a changelog entry, and nothing appeared in the changelog
  between 2026-05-18 and 2026-08-04. When it stops, affected users see a
  reauthentication prompt, and the method must be removed rather than repaired.
- An account user gets 50,000 complexity points per hour against an OAuth
  application's 300,000, so this method has one sixth of the headroom. The polling
  cadences are unchanged and stay far inside both.
- **Removal cannot revoke the token.** `invalidateRefreshToken` exists in the schema
  but returned `AUTHORIZATION/KT-CT-1111` when called as the signed-in account user on
  2026-08-04, and the provider documents `KT-CT-1111` and `KT-CT-1130` Unauthorized for
  it. `async_revoke` is therefore a documented no-op for this method rather than a
  request that can never succeed. Removal deletes the local copy, and the refresh token
  expires at the provider within seven days. Changing the OEJP password is the only way
  to invalidate it sooner. OAuth entries still revoke, at the OAuth revocation
  endpoint.
