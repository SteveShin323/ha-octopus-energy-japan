# ADR 0002: Scope one config entry to one login identity

Status: accepted
Date: 2026-07-29

## Context

One OEJP login may expose multiple current or historical accounts and supply
points. Account-per-entry setup duplicates credentials and coordinators and makes
resource changes difficult to reconcile. Email is not a stable account or OIDC
identity.

## Decision

Create one config entry for one OAuth issuer and authenticated viewer. Manage every visible
account and supply point under that entry. Derive the entry unique ID using an
installation-local secret and HMAC over issuer plus the authenticated viewer ID.

Account and supply-point devices use HMAC-derived identifiers. Raw provider
identifiers remain local only where required for API calls and ledger joins.

## Consequences

- Multiple accounts share one authentication and rate-aware scheduler.
- New supply points are discoverable without another config flow.
- Historical devices remain unavailable instead of being deleted.
- Pre-alpha account-per-entry installations must be reconfigured before alpha.

## Identity source, clarified 2026-08-04

The viewer identity comes from the API, with `viewer { id }` and the freshly issued
access token. It is not the `sub` claim of an ID token.

Two consequences follow, both desirable. The integration never parses or verifies a
JWT, so it is unaffected by the provider's ID-token signing algorithm — the
discovery document offers `HS256`, which a public client could not verify because it
holds no shared secret. And the identity is whatever the API resolves the token to,
which is exactly the property that decides which config entry owns which accounts.

An earlier version of this ADR and of the master design described the subject as the
OIDC `sub`. That was never what the code did.
