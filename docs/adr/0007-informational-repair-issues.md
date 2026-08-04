# ADR 0007: Informational repair issues and type-only diagnostics

Status: accepted
Date: 2026-08-04

## Context

A user reporting a problem with this integration has to attach something. The
obvious candidates all leak: logs carry provider messages, a screenshot of the
entity list carries device names, and a hand-written description carries an account
number more often than not.

The integration also observes conditions the user cannot act on. Readings arrive
late, a reading method turns out to be forbidden for their account, a stored month
fails to load, an optional commercial operation is not covered by their
authorization. Silence leaves the user to conclude the integration is broken.

Home Assistant offers two mechanisms: a diagnostics download per config entry, and
repair issues. Both are visible in the UI and both end up in bug reports.

## Decision

Diagnostics report only constants, counts, booleans, enumerated states,
installation-local HMAC identities, and UTC timestamps. No token, identity secret,
email address, name, address, account number, SPIN, meter or register identifier,
reading value, provider cost, balance, or bill amount is included.

A failure is reported by exception **class name** only. Provider and storage
messages can carry customer data, so the message is dropped rather than filtered.
Sanitized GraphQL error codes, types, and paths are kept, because the sanitizer
already guarantees those are bounded identifiers.

Repair issues are informational, `is_fixable=False`, and link to documentation.
None of the conditions can be resolved from inside Home Assistant, so no repair
flow is offered. Reauthentication is left entirely to Home Assistant's own prompt
rather than being duplicated as an issue.

Only reading capabilities raise a capability issue. A supply point without generic
devices or registers is ordinary. The reading-silence threshold is 36 hours,
comfortably beyond OEJP's normal several-hour delay and the 72-hour poll overlap.

Issues are keyed by config entry and cleared on removal, not on reload.

## Consequences

- A user can attach diagnostics to a public issue without reading them first.
- A maintainer sees which provider was selected, which capability was lost, how
  stale each series is, and how much correction has occurred, which is enough to
  triage most reports without asking for more.
- Values that would make some bugs easier to diagnose, such as an actual reading,
  are unavailable. That is accepted; a reproduction with synthetic data is the
  alternative.
- Because messages are dropped, an unclassified provider error shows only its type.
  The sanitized error code carries the useful part.
- Informational issues cannot be dismissed by fixing something, so a standing
  provider limitation stays visible. That is intended for a forbidden permission,
  which is a durable fact about the account rather than a transient fault.

## Alternatives rejected

- Redacting a full state dump with Home Assistant's `async_redact_data` was
  rejected because it requires enumerating every key that might appear, and a new
  provider field would leak by default. Building the report from an allow-list
  fails closed instead.
- Including exception messages behind a redaction pass was rejected because
  provider text is unbounded and no pattern set can be trusted to cover it.
- Fixable repair flows were rejected because none of these conditions has an
  in-app remedy, and a repair button that only dismisses the issue teaches users
  to dismiss real problems.
- A statistics-drift issue was rejected because drift is not observable without a
  second source of truth, and the projector already rebuilds from the ledger
  whenever a correction lands.
- Duplicating reauthentication as an issue was rejected because Home Assistant
  already prompts for it, and two notifications for one cause is noise.
