# ADR 0006: Independently optional commercial operations

Status: accepted
Date: 2026-08-04

## Context

Account status, balance, agreements, products, bills, and transactions live
behind OEJP permissions that differ from consumption permissions. An account
user may be able to read half-hourly readings while the same OAuth grant returns
an authorization error for billing, or the provider schema may not expose a
field at all.

These operations are also far less volatile than consumption and are not needed
for the Energy Dashboard. Fetching them on the consumption cadence, or during
config-entry setup, would add provider load and setup latency for data nobody is
waiting on.

Provider-issued cost sits in this scope too. Its OAuth permission, currency,
interval coverage, and correction semantics are unverified, so any projection of
it would assert facts the project has not established.

## Decision

Commercial data is fetched by a separate 12-hour coordinator, through three
independent optional GraphQL operations for overview, agreements, and billing.
Each operation carries its own privacy-safe availability status: `available`,
`partial`, `forbidden`, `unsupported`, or `failed`. Only sanitized error codes,
error types, and paths are retained.

An entity is unavailable unless its own feature succeeded, so one missing
permission cannot hide unrelated commercial values, and no commercial outcome
can affect consumption, ledger, aggregation, or statistics behavior.
Authentication failure is the sole exception: it propagates so Home Assistant
can request reauthentication.

The first commercial refresh is armed through the coordinator debouncer after
platform setup rather than awaited during setup.

Financial entities are disabled by default and expose bounded scalars only.
Monetary values are surfaced in whole `JPY` without scaling, recorded explicitly
as an unverified assumption.

Provider cost and product rate components are parsed and retained but not
projected to entities or statistics until the verification gate in
[`CONTRACT_AND_BILLING.md`](../CONTRACT_AND_BILLING.md) is closed.

## Consequences

- Partial OAuth permission degrades to fewer entities instead of a failed entry.
- Setup latency and first consumption data are unaffected by commercial queries.
- Commercial values can be up to 12 hours old, which suits contract and billing
  data and is documented for users.
- A transient failure hides commercial entities rather than presenting stale
  values as current.
- The parsed rate and cost models exist ahead of their projections, so PR 10
  diagnostics and a later probe can validate them without a schema change.
- If OEJP turns out to report a sub-yen minor unit, monetary entities are wrong
  by a constant factor until the recorded assumption is verified.

## Alternatives rejected

- One combined commercial query was rejected because a single authorization
  error would remove account, contract, and billing data together.
- Reusing the consumption coordinator was rejected because it would couple
  low-volatility data to the 30-minute reading cadence and to reading failures.
- Awaiting the first commercial refresh during setup was rejected because it
  delays entry setup for optional data and contradicts ADR 0004.
- Enabling financial entities by default was rejected because balances and bills
  are sensitive and are not needed for energy monitoring.
- Publishing an official-cost entity now, with a caveat in documentation, was
  rejected because an unverified currency denomination produces a silently wrong
  monetary state rather than a visible failure.
- Selecting a single product rate heuristically was rejected because rates are
  scoped by grid operator, region, and band, and no verified mapping to a
  supply point exists.
