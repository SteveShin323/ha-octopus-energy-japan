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

## 1a. Connecting them in the Energy dashboard

**Settings → Dashboards → Energy → Grid consumption → Add consumption**, then pick the
statistic named after the supply point — for example `OEJP supply point 1-1 Import
energy`. Export goes under **Return to grid** as `… Export energy`, when OEJP reports an
export direction.

Pick the **statistic**, not a period sensor. The period sensors are display
conveniences; the statistics are the correction-safe series.

Home Assistant's own energy validator accepts these as they are, with no issues, for
both directions. What it checks, and what this integration therefore guarantees:

| Requirement | How it is met |
|---|---|
| The statistic exists | published on every refresh from the ledger |
| `has_sum` | true; each row carries a cumulative `sum` |
| An energy unit it can convert | `kWh`, with the energy unit class |
| External statistic id | `octopus_energy_japan:sp_<digest>_<direction>_energy` |

Leave **cost** empty. This integration publishes no cost statistic, for the reasons in
[`CONTRACT_AND_BILLING.md`](CONTRACT_AND_BILLING.md). Entering a fixed price per kWh
there produces a number that no line of a Japanese bill supports, because the tariff is
tiered and carries a daily standing charge, a monthly fuel-cost adjustment, a renewable
levy, and tax.

There is no `total_increasing` meter sensor to select instead, by design. OEJP publishes
30-minute totals several hours late and revises them when a billing period closes. A
cumulative sensor fed from delayed, revisable data would either lag or jump backwards,
and the Energy dashboard would record both. External statistics can be rewritten in
place, which is what makes a correction safe.

### The name shown in the picker

The picker shows the statistic name and nothing else, so it follows the supply-point
device: one label, whatever the number of supply points. It contains no account number,
supply-point number, or address. Two supply points appear as `OEJP supply point 1-1` and
`OEJP supply point 1-2`, matching their devices.

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
publication is deliberately disabled until the OAuth permission, currency,
interval coverage, and correction semantics are verified from provider-confirmed
metadata or a scanner-approved probe. PR 9 reviewed that gate and recorded all
four items as still unmet, so publication remains disabled; the gate and the
evidence that would close it are tracked in
[`CONTRACT_AND_BILLING.md`](CONTRACT_AND_BILLING.md). User-entered flat-rate
estimates are not published.

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
