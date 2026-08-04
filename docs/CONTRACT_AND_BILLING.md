# Account, contract, and billing summaries

Status: normative implementation contract for Full Development Plan v3 PR 9
Reviewed: 2026-08-04

## 1. Purpose and authority

This document controls how optional OEJP account, agreement, product, bill, and
transaction data becomes Home Assistant state. It is more specific than
[`MASTER_TECHNICAL_DESIGN_V3.md`](MASTER_TECHNICAL_DESIGN_V3.md) within this
scope. The durable architectural decision is recorded in
[ADR 0006](adr/0006-optional-commercial-operations.md).

It also records the provider-cost verification gate that
[`ENERGY_STATISTICS.md`](ENERGY_STATISTICS.md) defers to this scope. That gate
is **not satisfied**; see [section 6](#6-provider-cost-verification-gate).

## 2. Independently optional operations

Commercial data is fetched through three separate GraphQL operations, each of
which may succeed or fail on its own:

| Feature | Operation | Content |
|---|---|---|
| `overview` | `AccountCommercialOverview` | account status, balance, overdue balance, agreement presence flags |
| `agreements` | `AccountCommercialAgreements` | market supply agreements, their periods, and the attached product |
| `billing` | `AccountCommercialBilling` | newest bill or invoice, newest posted transaction |

Every operation runs through the optional GraphQL execution path, so a partial
response is preserved instead of discarded. Each result is reduced to a
privacy-safe status:

| Availability | Meaning |
|---|---|
| `available` | the operation returned data with no errors |
| `partial` | the operation returned data alongside errors on optional fields |
| `forbidden` | customer OAuth permission does not cover the operation |
| `unsupported` | the provider rejected the document as invalid for this schema |
| `failed` | any other transport, server, or classification outcome |

Only sanitized GraphQL error codes, error types, and paths are retained.
Provider message text is never stored.

Authentication failure is the one outcome that is never absorbed. It propagates
so the config entry can request Home Assistant reauthentication.

Consumption never depends on commercial data. A `forbidden`, `unsupported`, or
`failed` commercial feature leaves reading, ledger, aggregation, and Energy
Dashboard behavior unchanged.

## 3. Refresh cadence and setup isolation

Commercial data uses its own coordinator with a 12-hour interval, matching the
Design v3 agreement, contract, tariff, and billing cadence.

The first commercial refresh is **never awaited during config-entry setup**. It
is armed through the coordinator debouncer only after devices are projected,
platforms are forwarded, and consumption background synchronization has started.
Setup latency and the first consumption refresh therefore remain independent of
these additional queries, as required by
[ADR 0004](adr/0004-non-blocking-runtime-synchronization.md).

The account roster is taken from discovery. A discovery update replaces the
roster for the next refresh rather than triggering an immediate extra fetch.

When a refresh fails, the last successfully retrieved values are retained and
every feature is marked `failed`, so affected entities become unavailable
instead of reporting stale data as current. The first failure with no previous
values reports nothing at all.

## 4. Deterministic parsing

GraphQL dictionaries never leave the parser. Entities and coordinators consume
only typed domain models: `AccountOverview`, `AgreementSummary`,
`ProductSummary`, `ProductRate`, `BillSummary`, and `TransactionSummary`.

The parsers are strict about anything that could silently corrupt a projection:

- an account number that does not match the requested account is rejected;
- a duplicated agreement with conflicting content is rejected, within a page and
  across pages;
- agreement pagination follows `endCursor` only, rejects a repeated cursor, and
  stops at a page safety limit rather than looping;
- `hasNextPage` without `endCursor` is rejected;
- a bill that reports both `totalCharges.grossTotal` and a conflicting
  `grossAmount` is rejected;
- a billing response containing more than the single requested bill or
  transaction is rejected;
- timestamps must be timezone-aware and are normalized to UTC; and
- monetary and numeric fields reject booleans and non-finite values.

The current agreement is selected on half-open UTC periods,
`valid_from <= instant < valid_to`, excluding terminated agreements. When
several agreements overlap, the latest `valid_from` wins.

## 5. Projected entities

All commercial entities belong to the account device and are named through
`has_entity_name` translations in English and Japanese.

Enabled by default:

| Entity | Feature | Device class |
|---|---|---|
| Account status | `overview` | diagnostic category |
| Current product | `agreements` | — |
| Current agreement start | `agreements` | timestamp |
| Current agreement end | `agreements` | timestamp |

Disabled by default, per the Design v3 rule that financial entities are opt-in:

| Entity | Feature | Device class |
|---|---|---|
| Account balance | `overview` | monetary |
| Overdue balance | `overview` | monetary |
| Latest bill amount | `billing` | monetary |
| Latest bill issued date | `billing` | date |
| Latest bill payment due date | `billing` | date |
| Latest transaction amount | `billing` | monetary |

An entity is unavailable unless its own feature is `available` or `partial`, so
one missing permission does not hide unrelated commercial values.

Bounded scalars only. Full bill, invoice, transaction, payment, or rate
collections are never placed in state attributes. Provider-rendered display
text such as a transaction title is parsed away rather than exposed.

Raw account numbers never appear in entity IDs, unique IDs, device identifiers,
or state. The account device is addressed by installation-local HMAC identity.

### Monetary unit

Monetary values are surfaced exactly as OEJP reports them, with the unit `JPY`
and no scaling. This was **confirmed** on 2026-08-04 by reconciling provider cost
against a real invoice for the same supply point: the implied unit price fell
inside the per-kWh band the invoice itself spans, while a sub-yen minor unit would
have been two orders of magnitude smaller. Integer fields such as `balance` and
`grossTotal` are whole yen for the same reason, since JPY has no circulating
sub-unit.

## 6. Provider cost verification gate

[`ENERGY_STATISTICS.md`](ENERGY_STATISTICS.md) models provider-issued official
cost but leaves publication disabled pending verification in this scope. The
required evidence is:

| Item | Status | Evidence |
|---|---|---|
| Account permission for cost fields | **met** | a real-account probe on 2026-08-04 returned `costEstimate` on `halfHourlyReadings` for every interval |
| Interval coverage | **met** | the same probe showed a `costEstimate` and a `version` on every returned interval |
| Currency and denomination | **met** | reconciled against a real invoice: the implied unit price fell inside the invoice's own per-kWh band, so values are whole yen |
| Correction semantics | **partly met** | `version` was observed switching from `DAILY` to `MONTHLY` exactly at the billing-period boundary, so intervals are reissued when a period closes; a before-and-after comparison of one interval is still outstanding |
| Generic provider parity | unmet | `SupplyPointType.readings` exposes no cost field at all, so provider cost is only reachable through the legacy operations that the fallback policy restricts |
| OAuth permission for cost fields | unmet | OEJP scope confirmation; the probe result above was obtained with the legacy login, not OAuth |

Three items are now closed and one is partly closed. Reading-level provider cost
is present, complete, and denominated in yen for this account under the legacy
login.

A new finding replaces denomination as the reason to stay disabled:
**`costEstimate` is not the billed amount.** The invoice combines a fixed daily
standing charge, three-tier energy pricing, a monthly fuel-cost adjustment, a
renewable levy, and consumption tax. A per-interval value cannot carry the fixed
daily charge, which was 8.5 percent of the invoice on its own, so summing
`costEstimate` under-reports the bill. Its exact composition cannot be determined
from this API because the provider serves a shorter history than one closed
billing period plus the open one.

Reconciling the readings against that invoice settled two things and left one
open. The comparison covered one complete billing period with no interval gaps.

Energy reconciles. Summed interval values came within 0.6 percent of the invoiced
kWh, which validates the reading pipeline end to end against a provider invoice.

Cost does not, and the reason is structural rather than a rounding gap.
Reconstructing `costEstimate` over the same period showed a per-kWh schedule with a
single cumulative-usage boundary at 300 kWh, switching mid-day on the day
cumulative usage crosses it. Two flat rates reproduce the summed value to within
0.04 percent, so that model is complete.

The invoice uses three tiers with boundaries at 120 and 300 kWh, plus a fixed daily
standing charge, a monthly fuel adjustment, a levy, and consumption tax.
`costEstimate` shows no step at 120 kWh and its single step is more than twice the
size of the invoice's, so it is not the billed tariff computed per interval. The
full comparison is recorded in [`API_CONTRACTS.md`](API_CONTRACTS.md).

`costEstimate` is therefore a provider estimate computed from its own simplified
rate model, not the customer's tariff applied per interval. Publishing it as the
Energy Dashboard cost would present a figure carrying provider authority that no
line of the bill supports. If it is published later it must be named and
documented as a provider estimate, never as the bill.

A second obstacle appeared with it. The generic reading API carries no cost field,
so provider cost is only available from the legacy operations, which the fallback
policy restricts to observed capability gaps. Publishing provider cost would mean
querying legacy operations outside that policy, which needs its own decision.

The OAuth item also cannot close until OEJP issues the application, because
account-user OAuth permission may differ from the legacy login's.

Provider-cost projection therefore stays disabled, with one gate and three
consequences:

1. official-cost external statistics remain disabled
   (`include_official_cost=False`);
2. no official-cost entity is created, for any period; and
3. product rate components are parsed and retained but not projected to an
   entity.

The third consequence has an additional reason, now better understood.
Introspection showed that `ApplicableRateType` does not carry a grid operator,
region, or band at all. Attribution is expressed through `category` and
`variantProfile`, and `variantProfile` is `JSONString`/`GenericScalar` — opaque
provider JSON rather than a typed contract. Selecting one rate therefore still
requires interpreting an untyped payload, so the integration parses and retains
rates but projects none. Design v3 also excludes `kWh x user-entered unit price`
cost estimation from the 1.0 scope until the tariff structure can be reproduced
faithfully.

Retaining the parsed rate model is deliberate: it is required for PR 10
diagnostics and it lets the eventual probe validate rate handling without
another schema change.

## 7. Regression evidence

| Contract area | Primary automated evidence |
|---|---|
| Independent optional operations and partial preservation | `tests/test_api_commercial.py` |
| Availability classification for every outcome | `tests/test_api_commercial.py` |
| Authentication propagation instead of absorption | `tests/test_api_commercial.py`, `tests/test_commercial_coordinator.py` |
| Agreement pagination, repeated cursor, and safety limit | `tests/test_api_commercial.py` |
| Conflicting duplicates within and across pages | `tests/test_api_commercial.py` |
| Strict field, timestamp, date, and monetary contracts | `tests/test_api_commercial.py` |
| Half-open UTC current-agreement selection | `tests/test_api_commercial.py` |
| Failure retains last values and marks features failed | `tests/test_commercial_coordinator.py` |
| First failure reports nothing | `tests/test_commercial_coordinator.py` |
| Roster changes apply on the next refresh | `tests/test_commercial_coordinator.py` |
| Commercial refresh never blocks setup | `tests/test_init.py` |
| Commercial coordinator shutdown on unload | `tests/test_init.py` |
| Entity values, device classes, and privacy | `tests/test_sensor.py` |
| Financial entities disabled by default | `tests/test_sensor.py` |
| Per-feature availability isolation | `tests/test_sensor.py` |
| English and Japanese translation parity | `tests/test_translations.py` |

`custom_components/octopus_energy_japan/api/commercial.py`,
`commercial_coordinator.py`, `entity.py`, and `sensor.py` hold 100 percent line
and branch coverage.

## 8. Out of scope

- provider-cost entities and statistics, and rate-component entities, until
  [section 6](#6-provider-cost-verification-gate) is closed;
- user-entered tariff modelling and derived cost estimation;
- payment method, direct debit, or account-management writes, because the
  integration is read-only;
- diagnostics and repair issues for commercial capability loss, which belong to
  PR 10; and
- user installation and troubleshooting documentation, which belongs to PR 11.
