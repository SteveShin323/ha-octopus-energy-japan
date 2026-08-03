# ADR 0005: Correction-safe external statistics

Status: accepted
Date: 2026-08-03

## Context

OEJP interval readings can arrive late, change after initial publication, or be
removed from a later authoritative snapshot. Home Assistant's Energy Dashboard
expects hourly external statistics with cumulative sums. A monotonic
"last timestamp plus new values" accumulator cannot correct historical rows and
will drift after restart, overlap polling, or provider revisions.

Raw account and supply-point identifiers must also remain absent from Recorder
metadata because external statistic IDs are user-visible and durable.

## Decision

The persistent interval ledger is the sole source of truth. A pure projection
normalizes physical intervals, allocates them into UTC hours, and recomputes
cumulative sums from the earliest locally available record. Normal insertions
and corrections replace rows from the earliest dirty hour. An authoritative
deletion clears only the affected supply-point direction through Recorder's FIFO
queue and then imports a full ledger-derived replacement.

Statistic IDs use an installation-local HMAC over account and supply-point
scope, followed only by direction and statistic kind. Production code uses Home
Assistant's external-statistics models and Recorder queue APIs and never writes
Recorder SQL directly.

Official provider cost remains disabled until its OAuth permission, currency,
coverage, and correction semantics are confirmed. Period entity states are not
used as Energy Dashboard truth.

## Consequences

- Reordering, duplicate fetches, corrections, deletion, and restart converge on
  the same result.
- Late readings can update historical energy and all later cumulative sums.
- Direction-scoped deletion rebuilds are more expensive than append updates but
  avoid stale Recorder rows without disturbing unrelated statistics.
- The cumulative baseline represents locally retained OEJP history rather than
  the physical meter's lifetime register.
- A failed publish can be retried from durable ledger state; no second reading
  database or persisted projection cursor is required.
- IDs remain stable within one Home Assistant installation and cannot be
  correlated across installations.

## Alternatives rejected

- Incrementing a restored total was rejected because historical corrections
  and overlap polling create irreversible drift.
- Publishing period sensors as `total_increasing` was rejected because calendar
  resets and incomplete coverage do not represent an authoritative meter total.
- Writing Recorder tables directly was rejected because it bypasses supported
  Home Assistant APIs and creates a brittle schema dependency.
- Fabricating zero rows for missing intervals was rejected because absence may
  mean delayed or unavailable provider data rather than zero consumption.
