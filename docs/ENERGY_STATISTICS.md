# Energy Dashboard statistics

Status: normative implementation contract for Full Development Plan v3 PR 8
Reviewed: 2026-08-03

## 1. Purpose and authority

This document controls how authoritative OEJP interval readings become Home
Assistant external long-term statistics. It is more specific than
[`MASTER_TECHNICAL_DESIGN_V3.md`](MASTER_TECHNICAL_DESIGN_V3.md) within this
scope. The durable architectural decision is recorded in
[ADR 0005](adr/0005-correction-safe-external-statistics.md).

Period entities such as today or this month remain user-facing convenience
projections. They are not the Energy Dashboard source of truth. The Energy
Dashboard consumes correction-aware external statistics rebuilt from the
persistent interval ledger.

## 2. Published series

The integration publishes one energy series for each discovered supply point
and successfully queryable direction:

- import energy in `kWh`; and
- export energy in `kWh` when OEJP exposes an export direction.

Each row is aligned to a UTC hour and contains:

- `state`: energy attributed to that hour; and
- `sum`: cumulative energy from the earliest locally available authoritative
  ledger record through that hour.

The cumulative baseline is local integration history, not the physical meter's
lifetime register. That is valid for Home Assistant external statistics because
the baseline remains deterministic and later rows preserve continuity.

The pure projector also models provider-issued official cost. Runtime cost
publication is deliberately disabled until PR 9 verifies the OAuth permission,
currency, interval coverage, and correction semantics from provider-confirmed
metadata or a scanner-approved probe. User-entered flat-rate estimates are not
published.

## 3. Stable private identity

Statistic identifiers have this public shape:

```text
octopus_energy_japan:sp_<installation-local HMAC>_<direction>_<kind>
```

The HMAC is derived from the installation secret plus account and supply-point
identifiers. Raw account numbers, SPINs, supply-point IDs, meter/register IDs,
addresses, email addresses, and OAuth data never enter statistic IDs or names.
The same Home Assistant installation retains stable IDs across reload and
restart; two installations cannot correlate the same OEJP identifier.

## 4. Deterministic projection

Projection uses normalized physical intervals from the ledger aggregation
layer. It therefore preserves the established provider precedence and avoids
double-counting equivalent generic, legacy, device, register, and unit series.
Non-kWh energy is normalized before projection. Billing-period
`intervalReadings` are excluded from physical energy statistics.

An interval entirely inside one UTC hour contributes its normalized energy to
that hour. An interval crossing an hour boundary is divided in proportion to
the number of seconds overlapping each hour. Intervals ending after the
projection time are omitted until complete.

Projection is deterministic under input reordering and exact duplication. All
cumulative sums are calculated from the complete locally available ledger,
even when only dirty replacement rows are sent to Recorder.

## 5. Insertions, corrections, and deletions

The coordinator retains the earliest unprojected changed interval per supply
point. It flushes affected ledger partitions before publishing statistics.

- A newly appended interval publishes its hour and later sums.
- A late interval or changed value/version/quality publishes from its earliest
  affected hour, retaining the preceding cumulative baseline.
- Multiple pending changes collapse to the earliest affected hour.
- A failed Recorder publication remains pending and is retried after the next
  successful data update. Repeated identical failures are logged once and
  recovery is logged once.
- Statistics failure does not discard consumption data or make the polling
  coordinator fail.

Recorder's additive external-statistics API cannot remove a row that ceased to
exist. If an authoritative provider snapshot deletes an interval, the affected
direction is therefore cleared through Recorder's supported FIFO queue API and
rebuilt from the complete local ledger. The clear and replacement imports are
queued in order without waiting on Recorder's worker thread. This prevents a
deleted hour from surviving indefinitely and avoids blocking synchronization if
Recorder is stopping. Other supply points and directions are not cleared.

A restart is safe even if publication failed immediately before shutdown:
initialization marks every enabled supply point for a complete deterministic
projection from durable ledger data.

## 6. Persistence and performance

Statistics do not maintain a second private reading database. The monthly
interval ledger remains authoritative. The projector loads known partitions
from the earliest locally retained month and streams only records belonging to
the target account and supply point into the pure projection layer.

Normal append/correction updates submit only rows at or after the dirty hour.
Deletion is intentionally more expensive because correctness requires a
direction-scoped clear and rebuild. Background reconciliation publishes only
after the ledger is durable and before its checkpoint advances.

The integration uses Home Assistant's public Recorder statistics models and
queue APIs. It does not write SQL or access Recorder tables directly in
production.

## 7. Availability and delayed data

External statistics contain only completed intervals actually held by the
ledger. Missing or delayed provider readings are not fabricated as zero. When a
late reading arrives, its historical hour and every affected cumulative sum are
updated. Energy Dashboard values may therefore increase or be corrected after
OEJP publishes revised data.

No claim of live, real-time, or current power is made.

## 8. Failure and privacy behavior

Statistics publishing may expose only safe HMAC identities, direction, unit,
kind, counts, timestamps, and sanitized exception categories. It must not log
raw ledger rows or provider identifiers. Recorder unavailability is isolated
from GraphQL synchronization, retained as pending work, and retried without a
tight loop.

The integration sends no statistic or telemetry to the project maintainer or
any external service.

## 9. Verification contract

Automated evidence must cover:

- hourly state and cumulative sums;
- cross-hour proportional allocation;
- import/export separation;
- complete and incomplete official-cost hours;
- unfinished intervals;
- dirty-range baseline retention;
- input-order independence and duplicate idempotency;
- correction replacement through a real Home Assistant Recorder harness;
- deletion-driven clear and rebuild;
- stable HMAC metadata without raw identifiers;
- ledger flush before publish and checkpoint advance;
- retry/recovery after Recorder failure; and
- setup/restart full projection.

The pure statistics and Recorder adapter modules require 100% line and branch
coverage. The complete repository retains the 95% line and branch gate.

## 10. User activation

There is no supported installation before OEJP issues and approves the public
OAuth application. After a release, Home Assistant discovers external
statistics by their `octopus_energy_japan:` source. Users select the relevant
import or export series in **Settings → Dashboards → Energy**. The final release
guide will include screenshots and clean-install verification.

The Japanese user guide is
[`ja/ENERGY_DASHBOARD.md`](ja/ENERGY_DASHBOARD.md).
