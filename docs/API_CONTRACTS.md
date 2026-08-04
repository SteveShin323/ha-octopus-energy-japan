# OEJP API contracts

This document records the discovery contracts used by the integration. The
official OEJP schema exposed to an authenticated customer remains the source of
truth. Sanitized probe fixtures must be regenerated when that schema changes.

## Reading contract provenance

The reading documents were validated on 2026-07-29 against the public
introspection schema and GraphQL validation endpoint at
`https://api.oejp-kraken.energy/v1/graphql/`. Validation reached the expected
`KT-CT-1112` authorization boundary without a schema or document error for all
six generic target/direction variants and both legacy operations. This proves
the public document shape, not customer OAuth permission or the presence of
data for a particular supply point.

The official
[OEJP GraphQL changelog](https://docs.oejp-kraken.energy/graphql/changelog/)
remains authoritative. In particular:

- `SupplyPointType.readings` was introduced in May 2025;
- import/export connections and device/register reading scopes replaced the
  original connection shape in October 2025;
- the `units` filter was added in January 2026;
- `timeGranularity` became nullable in February 2026; and
- the current `qualities` metadata replaced the removed singular quality
  fields in May 2026.

Protected customer schema and responses must still be confirmed with the
redacting local probe before alpha release.

## Resource discovery

The legacy customer hierarchy provides:

```text
viewer
└── accounts
    └── properties
        └── electricitySupplyPoints
            └── meters
```

The parser never selects the first account, property, supply point, or meter.
Every resource is retained in a deterministic typed hierarchy. Provider status
strings are normalized to `active`, `historical`, or `unknown`; unknown values
are not guessed.

## Generic device discovery

When introspection confirms that `Query.supplyPoint` and
`SupplyPointType.devices` are available, each discovered electricity supply
point is queried by `externalIdentifier` and the `ELECTRICITY` market. Generic
devices use `deviceIdentifier`; their registers use `registerIdentifier`.

Generic discovery is optional. An authorization or schema capability failure
does not invalidate working legacy discovery. Authentication, rate-limit,
transport, and malformed-response failures are not treated as capability
fallbacks.

## Capability registry

Capability introspection distinguishes:

- `supported`: the required root and object fields are visible;
- `unsupported`: introspection succeeded and a field is absent;
- `forbidden`: an authorization error prevented a reliable observation;
- `unknown`: the capability has not been probed.

Authorization is never classified as authentication and must not trigger OAuth
reauthentication.

## Pagination safety

Relay-style connections are collected with `hasNextPage` and `endCursor`.
Missing cursors, repeated cursors, and an excessive number of pages fail
closed. A caller must not infer completion from `hasPreviousPage`.

## Generic reading provider

The preferred provider calls `readings` at the most granular discovered level:

```text
DeviceRegister.readings
  -> Device.readings
  -> SupplyPointType.readings
```

Registers are preferred over their parent device and supply-point totals so the
same physical energy is not represented at multiple aggregation levels.
Import and export are fetched as separate Relay connections with `first <= 99`
and guarded cursor pagination.

The request contract is:

- `readingType: INTERVAL`;
- `timeGranularity: THIRTY_MIN`;
- `timezone: "UTC"`;
- energy units restricted to watt-hours, kilowatt-hours, and megawatt-hours;
- `intervalStart`, `intervalEnd`, `value`, and `units` required in every node;
  and
- `qualities { quality value count }` requested only when capability discovery
  confirms the field.

Provider timestamps are normalized to UTC and numeric values remain
`Decimal`. Quality entries are sorted deterministically. Conflicting duplicate
intervals in one response fail closed.

Quality metadata is optional. A structured authorization error whose path is
confined to `qualities` causes one retry without that field. Unscoped
authorization errors are not downgraded.

## Legacy reading provider

`halfHourlyReadings` is queried by account and bounded datetime range. The
parser retains `startAt`, `endAt`, `value`, `version`, and the OEJP-issued
`costEstimate`. Values are treated as kWh and, unless discovery identifies an
export series, as imported consumption.

`intervalReadings` is queried separately and retained under a distinct source.
Those records are billing-period aggregates and must never be added directly
to overlapping half-hour readings. The ledger and aggregation layer owns that
reconciliation rule.

## Strict provider fallback

Generic-to-legacy fallback is permitted only for:

- an observed unsupported or forbidden generic capability;
- a structured OAuth authorization gap scoped to a generic reading child
  field;
- `KT-CT-1113` or an equivalent structured disabled-field type; or
- a null/unconfigured generic device, register, or reading series after the
  requested supply point was found.

Authentication, rate limits, timeout/network/server failures, malformed data,
not-found identifiers, and unrecognized validation errors remain visible and
never trigger fallback.

Runtime authorization fallback is restricted to errors whose GraphQL path is
scoped to `readings`, `importReadings`, `exportReadings`, `devices`, or
`registers`. Missing-authentication (`KT-CT-1112`), account-authorization
(`KT-CT-4177`), unscoped authorization errors, and a null root `supplyPoint`
are propagated instead of being hidden by legacy fallback.

Every provider result records the selected provider, an allow-listed fallback
reason, the exact queried series (including series with zero returned
intervals), and the source families replaced by that successful batch. This
metadata lets the ledger delete stale topology and provider-transition rows
without guessing authority from a non-empty response.

## Credential rejection is reported as a validation error

Confirmed against a real account on 2026-08-04: `obtainKrakenToken` with an
incorrect password returns `errorType: VALIDATION` with
`errorCode: KT-CT-1138`, and the same document with the correct password returns
a token. OEJP therefore does not use `AUTHENTICATION` for a rejected credential.

Classification keys on the code before the type for exactly this reason. Without
that ordering the integration would treat an expired or revoked authorization as
an unsupported schema and mark the capability permanently unavailable instead of
requesting reauthentication.

## Optional commercial operations

Account status, agreements, and billing are three separate optional documents,
not one combined query. `AccountCommercialOverview` reads `account` status and
balances. `AccountCommercialAgreements` reads `marketSupplyAgreements` with its
product and rate connection and follows `endCursor` pagination.
`AccountCommercialBilling` reads the single newest `bills` node, using inline
fragments for `StatementType`, `PeriodBasedDocumentType`, and `InvoiceType`, plus
the single newest `transactions` node.

These documents have **not** been validated against the live schema. Unlike the
reading contracts above, they were derived from the published OEJP schema
documentation only. The `rates` shape, the bill fragment coverage, and the
denomination of every monetary field must be confirmed by the redacting local
probe before alpha release. The consequences of that gap, and the projections
withheld because of it, are recorded in
[`CONTRACT_AND_BILLING.md`](CONTRACT_AND_BILLING.md).

Each operation runs through the optional execution path. A per-operation
availability status distinguishes provider permission (`forbidden`) from schema
rejection (`unsupported`) and from transport or server outcomes (`failed`), so a
missing commercial permission never degrades consumption. Authentication errors
are propagated rather than recorded as an availability status.

## Privacy boundary

Raw account numbers, property identifiers, supply-point identifiers, meter
serial numbers, device identifiers, and register identifiers remain in typed
runtime data only where provider calls require them. Home Assistant device
identifiers use an installation-local HMAC and device names use neutral ordinal
labels. Raw provider identifiers are not written to states, attributes, logs,
or diagnostics.

Run the fixed local probes described in
[`FIXTURE_REDACTION.md`](FIXTURE_REDACTION.md) before changing any GraphQL
contract.
