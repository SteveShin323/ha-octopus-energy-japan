# Ledger, reconciliation, and calendar aggregation

This document defines the authoritative local data model used between OEJP
reading providers and Home Assistant entities/statistics. It implements
[ADR 0003](adr/0003-correction-aware-ledger.md). Runtime execution and backfill
ordering are defined by
[`RUNTIME_AND_ENTITIES.md`](RUNTIME_AND_ENTITIES.md) and
[ADR 0004](adr/0004-non-blocking-runtime-synchronization.md).

## Why raw intervals are persisted

OEJP readings can arrive late, be repeated, disappear from a later
authoritative response, or be revised after first publication. A restored
counter or a last-synchronized cursor cannot reproduce correct totals after
those events. The integration therefore persists normalized source intervals
and treats aggregates as disposable projections.

The logical interval key is:

```text
account
+ supply point
+ device/register
+ import/export direction
+ unit
+ provider source
+ UTC start
+ UTC end
```

Each `LedgerRecord` retains the normalized `EnergyReading`, provider observation
timestamp, revision, quality metadata, official cost when available, and a
local correction count.

## Authoritative snapshot merge

The caller supplies:

- the exact set of series covered by a query;
- the source families replaced by the selected provider;
- the half-open query window;
- all readings returned for those series and that window; and
- one shared observation timestamp.

The ledger applies these rules:

1. A new key is inserted.
2. An identical payload is a no-op, even if it was fetched again later.
3. A changed payload observed later replaces the existing record and increments
   its correction count.
4. A changed payload observed earlier is stale and cannot overwrite newer data.
5. Conflicting payloads with the same observation timestamp are rejected.
6. A previously stored record fully covered by the authoritative query, but
   absent from its response, is deleted unless that record was observed later.
7. Readings outside the requested series or window are rejected.

Provider selection is direction-specific. Each successful direction result
supplies its own authoritative series, source families, and observation time.
Runtime may combine independently successful direction results into one ledger
refresh, but a failed direction MUST NOT make another direction's partial target
set authoritative.

Before reconciliation, runtime uses `expand_authoritative_series` to add stored
series from the selected source families. This removes stale device/register
topology and prevents old generic readings from overriding a legacy fallback.
Legacy rows are not destroyed when generic readings are selected: generic data
wins overlapping energy intervals, while legacy data remains available for
explicitly modelled cost and historical fallback behavior. Legacy
`intervalReadings` are billing-period data, not a competing energy interval
source.

Input order does not affect the result. Replaying an identical snapshot is
idempotent. Hypothesis property tests enforce both invariants.

## Monthly persistence

Partitions use the UTC month containing `start_at`. Intervals may end in a
later month; the start month remains their stable owner.

```text
octopus_energy_japan.ledger.<entry_id>.<supply-point-hmac>.index
octopus_energy_japan.ledger.<entry_id>.<supply-point-hmac>.2026-07
```

Home Assistant `Store` provides atomic file replacement. Writes are debounced
per partition and can be flushed during unload. Stores are marked private.
Each backend is bound to one config entry and one installation-local HMAC
supply-point identity, so a damaged partition cannot contaminate another
supply point and raw provider identifiers never appear in storage filenames.
The current and previous UTC month stay loaded; older partitions load lazily
and are evicted after use.

The payload contains an explicit schema version. Schema 0 fixtures migrate to
schema 1 by converting the former single `quality` and `cost_estimate` fields
to normalized `qualities` and `official_cost` fields. Newer unknown schema
versions are never guessed.

Raw provider identifiers exist only in the user's private Home Assistant
storage because they are required to call OEJP and reconnect intervals to
their source series. They are not entity state, attributes, logs, or
diagnostics.

## Corruption isolation and repair

A malformed, missing-but-indexed, misplaced, or future-version partition is
marked corrupt. Other partitions remain available. Normal polling skips the
corrupt partition so a narrow overlap fetch cannot silently erase older data.

Repair requires an explicit full-partition backfill, after which
`async_replace_corrupt_partition` atomically replaces the partition and clears
its corruption state. The diagnostics and Repairs layers will surface this
state without including the damaged payload.

## Provider and unit reconciliation

Legacy `intervalReadings` are billing-period aggregates. They remain in the
ledger for official cost and contract features but are never summed with
half-hour energy.

For each physical interval:

1. Generic `SupplyPointType.readings` wins when generic and legacy half-hour
   sources overlap.
2. Within generic data, register series win over device series, and device
   series win over supply-point totals.
3. Distinct registers at the selected depth are summed.
4. Multiple unit representations of the same target are normalized to kWh and
   the most recently observed representation wins.
5. Equivalent representations observed at the same time must agree or the
   projection fails visibly.
6. Official cost is emitted only when every selected contributing record
   provides it.

This prevents source fallback, discovery topology changes, and unit changes
from double-counting energy.

## Calendar projections

Ledger timestamps stay in UTC. User-facing periods use `Asia/Tokyo`:

- today;
- yesterday;
- current ISO week, starting Monday;
- current month; and
- previous month.

Only intervals fully contained in a calendar window contribute to it.
Current-period windows end at the projection time. Intervals that have not
ended are excluded from both totals and the "latest reported interval", so
future or clock-skewed records cannot become user-visible data.

A calendar total is not exposed as complete merely because some ledger records
exist. Runtime tracks successful authoritative query coverage separately. A
period entity remains unknown until coverage spans the complete requested
calendar window through projection time. Missing intervals inside a successfully
queried authoritative window are not synthesized as zero.

## Synchronization plan

The fixed plan is:

| Operation | Window/cadence and execution |
|---|---|
| Blocking first refresh | latest 72 hours only |
| Consumption poll | every 30 minutes, latest 72 hours |
| Initial month backfill | background queue; previous and current JST month excluding the blocking 72-hour window |
| Daily reconciliation | background queue; previous and current JST month |
| Query chunk | no more than 7 days |
| Optional long backfill | background queue; up to 13 months |
| Discovery | 24 hours |
| Contract/tariff | 12 hours in its later coordinator |
| Billing | 12 hours in its later coordinator |

The first refresh never blocks on month backfill and never sleeps for startup
staggering. One persistent queue worker per config entry executes backfill and
reconciliation newest-first, with current month before previous month. One
shared request gate permits at most one in-flight GraphQL request per config
entry and gives regular polls priority before the next background item.

A background window becomes checkpoint-complete only after its authoritative
ledger changes are durably flushed. Restart reconstructs missing work from the
planner and private HMAC-scoped checkpoints. A successful authoritative empty
response completes query coverage; a failed or partial response does not.

Rate-limit recovery uses bounded exponential backoff with deterministic full
jitter, or a valid provider `Retry-After` value when present. Authentication,
authorization, validation, malformed-response, identifier, and ledger-invariant
failures are not retried as transient work. Installation-local startup
staggering applies only to the first background item.

## Release blocker: provider horizon versus requested authority

Status: open, found on a real account 2026-08-04

An authoritative snapshot deletes every stored interval that lies inside the
**requested** window and is absent from the response. `_legacy_authoritative_series`
derives authority from capabilities, not from whether anything was returned, so an
empty response is authoritative for its whole window.

OEJP truncates `halfHourlyReadings` to roughly a 30-day horizon without an error or
a pagination marker, as recorded in [`API_CONTRACTS.md`](API_CONTRACTS.md). The
daily reconciliation plans from the start of the previous JST month to now, so once
stored history ages past the provider horizon the next reconciliation covering it
receives nothing, treats that as authoritative, and deletes it.

The consequence is that the ledger can never retain more history than the provider
serves, which defeats its purpose: outliving provider retention so long-term
statistics stay stable. Statistics are then re-projected from the truncated ledger.

Resolving this requires a decision, not a patch, because each option trades a
different guarantee:

- derive the deletable range from the intervals actually returned, which stops
  honouring a genuine provider deletion at the edge of a window;
- treat an empty response as non-authoritative, which never removes a window the
  provider has fully withdrawn; or
- record an observed provider horizon and refuse to plan or delete beyond it, which
  adds state that must itself be corrected when the provider changes.

The horizon of the generic `SupplyPointType.readings` API must be measured first,
because the generic provider is preferred and its horizon may differ.
