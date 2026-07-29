# ADR 0002: Scope one config entry to one login identity

Status: accepted
Date: 2026-07-29

## Context

One OEJP login may expose multiple current or historical accounts and supply
points. Account-per-entry setup duplicates credentials and coordinators and makes
resource changes difficult to reconcile. Email is not a stable account or OIDC
identity.

## Decision

Create one config entry for one OAuth issuer and subject. Manage every visible
account and supply point under that entry. Derive the entry unique ID using an
installation-local secret and HMAC over issuer plus subject.

Account and supply-point devices use HMAC-derived identifiers. Raw provider
identifiers remain local only where required for API calls and ledger joins.

## Consequences

- Multiple accounts share one authentication and rate-aware scheduler.
- New supply points are discoverable without another config flow.
- Historical devices remain unavailable instead of being deleted.
- Pre-alpha account-per-entry installations must be reconfigured before alpha.
