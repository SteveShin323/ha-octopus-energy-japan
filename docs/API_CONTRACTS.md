# OEJP API contracts

This document records the discovery contracts used by the integration. The
official OEJP schema exposed to an authenticated customer remains the source of
truth. Sanitized probe fixtures must be regenerated when that schema changes.

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
