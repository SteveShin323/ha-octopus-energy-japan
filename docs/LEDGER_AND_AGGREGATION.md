# Ledger, reconciliation, and calendar aggregation

This document defines the authoritative local data model used between OEJP
reading providers and Home Assistant entities/statistics. It implements
[ADR 0003](adr/0003-correction-aware-ledger.md).

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

`ReadingBatch` reports both the queried series and the source families for
which the successful batch is authoritative. Before reconciliation, runtime
uses `expand_authoritative_series` to add stored series from those families.
This removes stale device/register topology and prevents old generic readings
from overriding a legacy fallback. Legacy rows are not destroyed when generic
readings are selected: generic data wins overlapping energy intervals, while
legacy data remains available for explicitly modelled cost and historical
fallback behavior. Legacy `intervalReadings` are billing-period data, not a
competing energy interval source.

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

`legacy intervalReadings` are billing-period aggregates. They remain in the
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

## Synchronization plan

The window planner enforces:

| Operation | Window/cadence |
|---|---|
| Consumption poll | every 30 minutes, latest 72 hours |
| Initial backfill | current and previous JST month |
| Daily reconciliation | current and previous JST month |
| Query chunk | no more than 7 days |
| Optional long backfill | up to 13 months |
| Discovery | 24 hours |
| Contract/tariff | 12 hours |
| Billing | 12 hours |

Rate-limit recovery uses bounded exponential backoff with deterministic full
jitter, or a provider `Retry-After` value when present. Installation-local
startup staggering prevents every coordinator from calling OEJP at once.

The runtime coordinator added in the next phase owns execution of these plans.
The planner itself performs no network or storage mutation.
