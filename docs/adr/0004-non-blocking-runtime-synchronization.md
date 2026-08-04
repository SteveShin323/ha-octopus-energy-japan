# ADR 0004: Use a short blocking bootstrap and a persistent background sync queue

Status: accepted
Date: 2026-07-31

## Context

The integration needs recent readings before entities can be created, but a full
previous-and-current-month reconciliation may require many paginated GraphQL
requests for every supply point, device, register, and direction. Performing all
of that work inside `async_config_entry_first_refresh()` would make setup time
scale with account topology and provider rate limits.

The runtime must also survive restarts, delayed data, corrections, partial
permissions, historical resource transitions, and rate-limit responses without
losing deterministic ledger behavior or producing a request burst.

## Decision

The blocking first refresh fetches only the most recent 72 hours for enabled
supply points. It performs no startup sleep and no month backfill.

After platforms are forwarded, one persistent background worker per config entry
fills the previous and current Japanese calendar month and performs daily
reconciliation. Work is split into at-most-seven-day supply-point-and-direction
request scopes, serialized through one shared request gate, checkpointed by
direction only after the affected ledger data is durably flushed, and
reconstructed after restart.

One request scope can satisfy several obligations, such as initial backfill and
daily reconciliation. The queue deduplicates by supply point, direction, and
window, then coalesces reason/generation obligations so equivalent GraphQL work
is never repeated merely because two schedules requested it.

Regular polls have priority over queued background work. Startup staggering
applies only to the background worker. Rate limits and transient failures retry
with `Retry-After` or bounded jittered exponential backoff. Authentication,
authorization, validation, malformed-response, identifier, and ledger-invariant
failures do not retry as transient work.

Provider selection and entity direction are determined per supply point and per
direction from successful authoritative results. Global schema capability does
not by itself create an entity. Import completion cannot mark export history
complete, and export completion cannot mark import history complete.

The complete executable contract is
[`ARCHITECTURE.md`](../ARCHITECTURE.md).

## Consequences

- Config-entry setup has a bounded recent-data scope and does not wait for two
  calendar months of history.
- Month and week entities may remain unknown until their own direction's
  authoritative query coverage reaches the complete calendar window.
- A private versioned checkpoint store with direction-specific state is required
  for each initialized supply point.
- Background synchronization, ledger mutation, checkpoint persistence, and
  snapshot publication require explicit ordering and cancellation tests.
- Provider observations must be direction-specific so partial generic support
  does not discard successful directions or create unsupported entities.
- Queue obligation coalescing is required to prevent initial and daily schedules
  from duplicating the same request.
- The runtime becomes more complex than one sequential coordinator loop, but the
  complexity is isolated and testable rather than hidden in setup latency or
  unbounded retry behavior.
