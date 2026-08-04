# Diagnostics and repair issues

Status: normative implementation contract for Full Development Plan v3 PR 10
Reviewed: 2026-08-04

## 1. Purpose and authority

This document controls what the integration reveals about itself: the diagnostics
download attached to a config entry, and the repair issues it raises in Home
Assistant. It is more specific than
[`MASTER_TECHNICAL_DESIGN_V3.md`](MASTER_TECHNICAL_DESIGN_V3.md) within this scope.
The durable decision is recorded in
[ADR 0007](adr/0007-informational-repair-issues.md).

Diagnostics exist so a user can attach one file to a public issue without leaking
anything about themselves. That constraint is the whole design.

The Japanese user-facing companion is [`ja/DIAGNOSTICS.md`](ja/DIAGNOSTICS.md).

## 2. What diagnostics contain

Every value is a constant, a count, a boolean, an enumerated state, an
installation-local HMAC identity, or a UTC timestamp.

| Section | Content |
|---|---|
| `integration` | domain, integration version, Home Assistant version, ledger schema version |
| `config_entry` | entry and minor version, source, whether a token and an auth implementation exist, how many historical resources are selected |
| `runtime` | load state, last refresh outcome, last exception **type name**, poll interval, capability registry, resource counts, correction count, corrupt-partition count |
| `directions` | per series: HMAC account and supply-point identity, direction, queryable, stale, last success, error class, coverage window, background window count |
| `providers` | per series: which provider was selected and the allow-listed fallback reason |
| `aggregation` | generation time, timezone, projection count, recent interval count, largest data delay |
| `commercial` | per feature: availability plus sanitized GraphQL error codes, types, and paths |

## 3. What diagnostics never contain

- OAuth access tokens, refresh tokens, or the installation identity secret;
- email addresses, names, or postal addresses;
- account numbers, SPINs, supply-point identifiers, meter or register identifiers;
- reading values, provider cost, balances, or bill and transaction amounts;
- provider message text, only its sanitized error code, type, and path; and
- exception messages. Only the exception **class name** is reported, because a
  provider or storage message can carry customer data.

A regression test asserts each of these against a fixture built from realistic
customer values, including that a monetary amount does not appear anywhere in the
serialized report.

## 4. Why the repair issues are informational

Every condition below originates with the provider or with local storage. None can
be fixed by an action inside Home Assistant, so none offers a repair flow. Offering
a button that cannot repair anything would be worse than an explanation.

Reauthentication is deliberately **not** duplicated as an issue. When
authorization fails the integration raises `ConfigEntryAuthFailed`, and Home
Assistant's own reauthentication prompt is a better experience than a second
notification that says the same thing.

Issues are keyed by config entry and cleared when the entry is removed, not when it
is reloaded, so a restart does not hide a standing problem.

## 5. The issues

### ledger-partitions-corrupt

A stored month of readings failed to load and was quarantined so the rest of the
history stays usable. Severity is error, because totals and Energy Dashboard
statistics for those months may be incomplete.

Removing and re-adding the integration re-downloads the current and previous
month. OEJP still serves older intervals, so a wider recovery is possible in
principle; the integration does not request one on its own today.

### readings-silent

A queryable reading series has produced nothing for longer than 36 hours. OEJP
publishes half-hourly readings with a normal delay of several hours, and the
regular poll already overlaps 72 hours, so a shorter threshold would report
ordinary provider lag as a fault. A direction the provider does not expose is never
counted.

No user action helps. The integration keeps retrying and backfills once the data
appears.

### capability-unavailable

A reading method is unsupported or forbidden for this account. Only reading
capabilities are reported. A supply point without generic devices or registers is
ordinary and is not a fault, so optional topology gaps stay out of the UI.

If every reading method is listed, no new readings can be collected at all.

### commercial-permission-missing

The authorization does not cover an optional commercial operation. Consumption,
totals, and Energy Dashboard statistics are unaffected, and only the entities for
the listed information stay unavailable.

This is expected rather than exceptional: `marketSupplyAgreements` was already
observed as forbidden for an account-user login.

## 6. Regression evidence

| Contract area | Primary automated evidence |
|---|---|
| No customer value appears in diagnostics | `tests/test_diagnostics.py` |
| Exception reported by type only | `tests/test_diagnostics.py` |
| Diagnostics work before the runtime loads | `tests/test_diagnostics.py` |
| Every documented section is present | `tests/test_diagnostics.py` |
| Each issue is raised on its own condition | `tests/test_issues.py` |
| A recovered condition clears its issue | `tests/test_issues.py` |
| Provider lag inside the threshold is not reported | `tests/test_issues.py` |
| A non-queryable direction is not reported | `tests/test_issues.py` |
| Optional topology gaps are not reported | `tests/test_issues.py` |
| Removing an entry clears every issue | `tests/test_issues.py` |
| English and Japanese issue text parity | `tests/test_translations.py` |

## 7. Out of scope

- repair flows, because no condition here is fixable in place;
- a statistics-drift issue, because drift is not observable without a second
  source of truth, and the projector rebuilds from the ledger on every correction;
- exception translations, because the integration exposes no actions and raises no
  user-facing `HomeAssistantError`; and
- external telemetry, which the project does not implement.
