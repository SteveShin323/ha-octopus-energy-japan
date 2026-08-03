# PR 7 completion audit

Status: completion evidence for PR #14, ready for review
Reviewed: 2026-08-03

This audit verifies the implementation against
[`RUNTIME_AND_ENTITIES.md`](RUNTIME_AND_ENTITIES.md),
[`LEDGER_AND_AGGREGATION.md`](LEDGER_AND_AGGREGATION.md), and
[ADR 0004](adr/0004-non-blocking-runtime-synchronization.md). It records
evidence; it does not weaken those normative contracts.

## Adversarial findings closed

The final audit found and corrected three runtime defects:

1. The first 72-hour refresh also planned month history internally. History is
   now planned only after devices and platforms are set up and background sync
   is explicitly started.
2. A transiently failed direction remained entity-available because only its
   `queryable` flag was checked. Directional entities now also require a
   non-stale current result; the point status entity remains independently
   available.
3. A generation-scoped permanent background failure was reconsidered on every
   successful poll. Reconsideration is now limited to a new discovery generation
   or a new daily generation, preventing poll-driven retry churn.
4. The supply-point status entity inherited direction-refresh availability and
   became unavailable during an otherwise isolated reading failure. It now
   remains available from the last discovered present/enabled lifecycle while
   only affected directional entities become unavailable.
5. Device disabling used a runtime enum-name compatibility shim. The integration
   now targets the supported Home Assistant `DeviceEntryDisabler` API directly.

## Mandatory regression evidence

| Contract area | Primary automated evidence |
|---|---|
| Bounded first refresh and post-setup background start | `tests/test_coordinator.py`, `tests/test_init.py`, `tests/test_sync.py` |
| Permanent-only status setup and transient setup retry | `tests/test_coordinator.py`, `tests/test_init.py` |
| Partial initialization cleanup | `tests/test_coordinator.py`, `tests/test_init.py` |
| One-operation gate across auth refresh and topology | `tests/test_api_auth.py`, `tests/test_init.py` |
| Candidate direction rules and no capability-only entity | `tests/test_coordinator.py`, `tests/test_sensor.py`, `tests/test_binary_sensor.py` |
| Direction-scoped generic results and fallback | `tests/test_api_readings.py` |
| Empty export success and legacy unknown-direction handling | `tests/test_api_readings.py`, `tests/test_coordinator.py` |
| Window ordering, coalescing, priority, and supersession | `tests/test_background_sync.py`, `tests/test_sync.py` |
| Direction/reason/generation checkpoint isolation | `tests/test_background_sync.py` |
| Daily barriers and completed-through state | `tests/test_background_sync.py` |
| Ledger flush before checkpoint | `tests/test_coordinator.py` |
| Restart reconstruction and historical coverage | `tests/test_background_sync.py`, `tests/test_coordinator.py` |
| New direction history and poll preemption | `tests/test_coordinator.py` |
| Retry-After, jitter, defer, rate barrier, and no-spin | `tests/test_api_client.py`, `tests/test_sync.py`, `tests/test_sync_runtime.py`, `tests/test_coordinator.py` |
| Multiple points/directions/windows and partial success | `tests/test_coordinator.py`, `tests/test_aggregation.py` |
| 24-hour discovery and lifecycle queue cancellation | `tests/test_coordinator.py` |
| Active/historical/missing/reappearing resource lifecycle | `tests/test_runtime.py`, `tests/test_config_flow.py` |
| Disabled-state aggregation exclusion with retained stores | `tests/test_coordinator.py` |
| Dynamic entity addition without duplicates | `tests/test_sensor.py`, `tests/test_binary_sensor.py` |
| Coverage-gated period state, including authoritative zero | `tests/test_aggregation.py`, `tests/test_sensor.py` |
| Shutdown, unload failure, and one-worker recovery | `tests/test_init.py`, `tests/test_coordinator.py` |
| Stable HMAC identity across lifecycle and reload boundaries | `tests/test_identity.py`, `tests/test_runtime.py`, `tests/test_init.py` |
| Authentication/transient/permanent classification | `tests/test_api_errors.py`, `tests/test_api_client.py`, `tests/test_coordinator.py` |
| English/Japanese translation parity | `tests/test_translations.py` |
| Provider identifier and credential privacy | `tests/test_runtime.py`, `tests/test_sensor.py`, `tests/test_binary_sensor.py`, `tests/test_probe.py` |

The full suite also exercises OAuth rotation and revocation, strict and optional
GraphQL response paths, schema contracts, pagination, ledger migrations,
corruption isolation, correction ordering, JST calendar boundaries, and
property-based reconciliation invariants.

## Quality and scope closure

- Ruff lint and formatting, strict mypy, pytest line/branch coverage, Hassfest,
  HACS validation, link checks, Security, CodeQL, Codecov, and dependency review
  remain required checks.
- No production credential is stored in CI. Provider contract fixtures are
  synthetic and sanitized.
- PR 7 contains no external recorder-statistics implementation, tariff/billing
  entity implementation, diagnostics download, or Repairs implementation.
- Production OAuth metadata and OEJP public-client approval remain release
  blockers, not fixture-based PR 7 implementation blockers.
- The final integration head passed every required check and PR #14 was marked
  ready for review. This delivery process does not merge PR #14; PR 8 starts
  from `main` only after that integration PR is merged.
