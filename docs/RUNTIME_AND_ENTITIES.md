# Runtime and entity projections

This document describes the Home Assistant runtime boundary implemented after
the reading-provider and ledger layers. The normative security and release
requirements remain in
[`MASTER_TECHNICAL_DESIGN_V3.md`](MASTER_TECHNICAL_DESIGN_V3.md).

## Ownership

One OAuth login-scoped config entry owns:

- one shared authenticated GraphQL client;
- the latest typed account and capability discovery snapshot;
- one reading coordinator;
- one private monthly ledger per enabled supply point; and
- account and supply-point devices identified by installation-local HMACs.

The coordinator is the only runtime layer allowed to combine provider results
with persistent ledger state. Entities receive an immutable
`OejpCoordinatorData` snapshot and do not parse GraphQL dictionaries, retain
OAuth tokens, or perform API calls.

## Refresh sequence

Each regular refresh performs the following sequence:

1. refresh account and supply-point discovery when its 24-hour cadence is due;
2. create ledgers for newly enabled active or explicitly selected historical
   supply points;
3. query the current 72-hour overlap, or the current and previous month for an
   initial/daily reconciliation;
4. route generic readings to the legacy provider only for an allow-listed
   capability or permission mismatch;
5. reconcile each authoritative provider batch into its supply-point ledger;
6. load the current and previous Japanese calendar month;
7. produce immutable `Asia/Tokyo` calendar aggregates; and
8. notify Home Assistant entities.

An OAuth authentication failure raises `ConfigEntryAuthFailed` and starts
Home Assistant reauthentication. Transport, rate, schema, and ledger failures
raise `UpdateFailed`; they do not incorrectly discard the OAuth authorization.

Ledger writes are debounced during normal operation and synchronously flushed
when the config entry unloads.

## Discovery lifecycle

Active and unknown resources are enabled automatically. Historical accounts and
supply points remain discoverable but are disabled by the integration until the
user explicitly selects their installation-local identities in the reconfigure
flow.

New supply points create devices and entities after the next successful
discovery refresh. A disappeared supply point is not deleted: its existing
entities become unavailable, preserving entity and recorder history.

Raw account numbers, supply-point identifiers, SPINs, addresses, and customer
names are not used in device names, entity unique IDs, states, or attributes.
They remain private runtime/storage join keys only.

## Entity source of truth

The following entities are enabled for each available import/export direction:

| Entity | Source |
|---|---|
| Latest reported interval energy | Latest completed ledger interval |
| Today | Japanese local-day ledger aggregate |
| Yesterday | Previous Japanese local day |
| This week | Monday-to-current-time aggregate |
| This month | Japanese calendar month |
| Last month | Previous Japanese calendar month |
| Latest reading timestamp | Provider interval end time |
| Data delay | Coordinator time minus latest interval end |
| Data available | Whether at least one completed interval is present |
| Supply-point status | Normalized discovery lifecycle |

The integration deliberately avoids the terms “live”, “real-time”, and
“current power”. OEJP interval readings can be delayed and corrected.

Period sensors are convenience projections. They are not the authoritative
source for the Home Assistant Energy Dashboard. The Energy Dashboard uses the
external statistics projector implemented in the following development phase.

## Dynamic entity creation

Sensor platforms keep a set of HMAC-derived unique IDs already submitted to
Home Assistant. A successful discovery refresh may add entities for new supply
points or newly available export directions, but it cannot submit duplicates.

Entities are grouped under a supply-point device, which is linked to its parent
account device. Meter and register identifiers remain part of the internal
reading series key unless they become independently manageable resources.

## Test contract

Runtime tests must cover:

- setup, first refresh, unload, and ledger flush;
- authentication versus transient failure handling;
- active and explicitly selected historical resources;
- multi-account and multi-supply-point iteration;
- provider observation and authoritative reconciliation;
- import/export entity creation without duplicates;
- deterministic state, device class, unit, and availability;
- HMAC unique IDs and absence of provider identifiers; and
- English/Japanese translation completeness.
