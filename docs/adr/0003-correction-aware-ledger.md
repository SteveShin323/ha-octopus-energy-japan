# ADR 0003: Persist interval readings in a correction-aware ledger

Status: accepted
Date: 2026-07-29

## Context

OEJP readings may arrive late or be revised. Restoring and incrementing a single
total, or storing only the last synchronization cursor, cannot reliably handle
duplicates, corrections, missing intervals, restarts, or historical
reconciliation.

## Decision

Persist normalized interval readings in versioned monthly Home Assistant Store
partitions. Identify a record by series plus UTC start and end time. Preserve
value, revision, quality, official cost, source, and fetch metadata.

Derive aggregates and Home Assistant external statistics from the ledger.
Reproject statistics from the earliest affected hour after a correction.

## Amendment, 2026-08-13: absence is not a withdrawal

A reading the provider stops returning inside a queried window used to be deleted, on the
reasoning that the provider is authoritative for what it was asked about. That reasoning
holds only if the answer arrived. It often does not.

A window spanning several months is split per partition before merging, so a partition ends
up with an empty snapshot whenever the response stopped short of it — a page that ended
early, a transient `KT-CT-7899`, a partial read. Every one of those looked identical to the
provider withdrawing the whole month.

On 2026-08-13 that erased 35 days of a real account's history in one afternoon, across two
passes, while the provider still returned all 48 readings for every one of those days when
asked again. It also produced a cumulative that went backwards, which the Energy Dashboard
drew as a single day of −62.1 kWh.

**A partition whose snapshot came back empty is now left alone.** Deletion still applies
inside a partition that returned something, because there the provider did answer and a
missing interval is a real withdrawal. The skipped partitions are reported on
`CorrectionResult.skipped_empty_partitions`.

The cost of this is that a month the provider genuinely empties keeps its stored readings.
That is the correct trade: a stale reading overstates a total, while a deleted one destroys
history that may be unobtainable.

## Amendment, 2026-08-24: an interval's key must be validated, not just trusted

"Identify a record by series plus UTC start and end time" assumed a reading restating an
existing interval reports the same start and end it did before. That held until it did not.

On a real account, several half-hourly readings arrived with `endAt` a few minutes after
`startAt` instead of the usual thirty — internally inconsistent with their own declared
granularity. Nothing checked that. The reading carried the same `start_at` as the correct
30-minute reading already on file, but because `end_at` differed, its key differed too, and
both the reading parser's deduplication and the ledger's merge treated it as a different
interval rather than the same slot restated. Neither replaced the other; both were kept,
doubling that half hour's energy and every later cumulative total. Re-querying the same
interval afterward returned a clean 30-minute reading, so this looks like a transient
upstream fault rather than something this integration produces on its own.

Raising on the mismatch — this repository's usual answer to a response it cannot trust —
would have been worse here. A malformed response is triaged as
`DirectionErrorClass.INVALID_RESPONSE`, scoped to the whole supply point, so refusing this
one reading would have refused the roughly 1,499 good ones fetched alongside it.

**A reading is now checked against its own declared granularity before it can compete for a
ledger slot at all.** One whose span does not match — 30 minutes for `THIRTY_MIN`, and so on
for every other fixed granularity — is dropped and logged rather than kept, so it never
reaches a key comparison it would fail silently. Restating an interval with a *correct* span
still replaces it exactly as this ADR describes; what changed is that a reading is no longer
trusted to be well-formed just because the provider sent it.

## Consequences

- Results are reproducible and idempotent.
- Storage migration and damaged-partition recovery become required features.
- Current and previous months can stay in memory while older data loads lazily.
- Ledger merge and statistics projection are critical modules with 100% test
  coverage targets and property-based tests.
