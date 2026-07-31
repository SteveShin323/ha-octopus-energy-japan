# Runtime execution and entity projection specification

Status: normative implementation contract for Full Development Plan v3 PR 7
Reviewed: 2026-07-31

This document defines the only permitted runtime design for the Home Assistant
integration. It is a subordinate specification of
[`MASTER_TECHNICAL_DESIGN_V3.md`](MASTER_TECHNICAL_DESIGN_V3.md) and implements
[ADR 0004](adr/0004-non-blocking-runtime-synchronization.md). Where this document
is more specific about runtime behavior, this document controls.

The terms **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative. PR 7 is
not complete while any decision in this document remains unimplemented,
untested, or contradicted by another non-archived document.

## 1. Scope

PR 7 owns:

- config-entry runtime setup and unload;
- bounded reading synchronization;
- initial and daily historical reconciliation execution;
- discovery-driven resource lifecycle transitions;
- persistent direction-specific backfill checkpoints;
- per-supply-point and per-direction provider observations;
- immutable coordinator snapshots;
- account and supply-point devices;
- consumption, timestamp, delay, status, and data-availability entities; and
- English and Japanese entity translations.

PR 7 does not implement external recorder statistics, tariff or billing
entities, diagnostics downloads, Repairs issues, or release packaging beyond
what is required for current validation. Those later phases MUST consume the
runtime and ledger contracts defined here rather than replacing them.

## 2. Required runtime components

One OAuth login-scoped config entry MUST own exactly one of each of the
following:

- shared `AuthSession`;
- shared authenticated GraphQL client;
- request gate with a maximum of one in-flight GraphQL request;
- latest typed discovery and capability snapshot;
- `OejpDataUpdateCoordinator`;
- background `OejpSyncQueue` worker;
- one persistent interval ledger per initialized supply point; and
- one persistent backfill checkpoint store per initialized supply point, with
  independent direction state.

The pure window planner remains in `sync.py`. Network execution, retry state,
queue mutation, and checkpoint persistence MUST be implemented outside the pure
planner, in `sync_runtime.py` or an equivalently isolated module.

Entities MUST consume immutable coordinator data. Entities MUST NOT call OEJP,
parse GraphQL dictionaries, retain OAuth tokens, mutate ledgers, or infer
provider capability independently.

## 3. Fixed scheduling and request constants

The implementation MUST use these values unless a later accepted ADR changes
them:

| Constant | Required value |
|---|---:|
| Regular poll interval | 30 minutes |
| Regular poll overlap | most recent 72 hours |
| Maximum reading query window | 7 days |
| Discovery interval | 24 hours |
| Blocking setup backfill | none |
| Initial background backfill | previous and current JST month, excluding the blocking 72-hour window |
| Daily background reconciliation | previous and current JST month |
| Maximum GraphQL concurrency per config entry | 1 |
| GraphQL request timeout | 30 seconds |
| Background-start stagger | deterministic 0 to 5 minutes |
| Retry base | 30 seconds |
| Retry maximum | 1 hour |
| Attempts per background activation | 5 |
| Deferred retry after five transient failures | 6 hours |
| Optional long backfill limit | 13 months |

No unbounded `asyncio.gather` is permitted for GraphQL calls. All core discovery,
generic topology, reading, and optional-operation requests made by one config
entry MUST pass through the same request gate.

The background-start stagger MUST apply only before the first background item.
It MUST NOT delay config-entry setup, the first coordinator refresh, or a regular
30-minute poll.

## 4. Config-entry setup sequence

`async_setup_entry` MUST perform these steps in order:

1. resolve the Home Assistant OAuth implementation and OEJP metadata;
2. construct the shared auth session and validate or refresh the token;
3. construct the gated authenticated GraphQL client;
4. perform strict core discovery and optional capability/topology discovery;
5. load the installation-local identity secret;
6. construct `OejpRuntimeData`, coordinator, ledgers as needed, sync queue, and
   checkpoint stores;
7. run the first coordinator refresh using only the regular 72-hour poll window;
8. project account and supply-point devices;
9. forward the sensor and binary-sensor platforms; and
10. start the background sync worker.

The first refresh MUST NOT execute `SyncWindowPlanner.initial()`, daily
reconciliation, optional long backfill, or a startup sleep.

If there are no enabled supply points, setup MUST succeed with an empty reading
snapshot and no background reading items.

If at least one enabled supply point completes a direction of its 72-hour
bootstrap, setup MAY succeed with other points or directions marked unavailable.
If every enabled point fails, the failure mapping in section 10 applies.
Authentication failure always aborts the entry-wide setup.

Any setup failure after runtime allocation MUST cancel created tasks, flush and
close every initialized store, clear `entry.runtime_data`, and avoid forwarding
platforms. Cleanup MUST be idempotent and covered by a test that fails after at
least one ledger has been initialized.

## 5. Sync work model

A background queue item MUST identify exactly one request scope:

```text
supply-point identity
+ import/export direction
+ half-open UTC start/end window of at most 7 days
```

The item MUST also carry one or more obligations. An obligation is:

```text
sync reason + generation
```

Required reasons and priorities are:

| Priority | Reason |
|---:|---|
| 0 | regular poll; coordinator-owned, not persisted as queue work |
| 10 | daily reconciliation |
| 20 | initial current-month backfill |
| 30 | initial previous-month backfill |
| 40 | optional long backfill |

The queue deduplication key MUST be supply point, direction, start, and end. It
MUST NOT include reason or generation. Enqueuing the same request scope adds its
obligation to the existing item and changes the effective priority to the
highest-priority obligation. This coalesces initial backfill and daily
reconciliation instead of issuing duplicate GraphQL requests.

When a generation becomes obsolete, only that obligation is removed. The queue
item is removed only when no current obligation remains. One successful
authoritative direction result satisfies every current obligation attached to
that request scope and updates all applicable checkpoints.

Only one background worker is allowed per config entry. Only one GraphQL request
is allowed in flight. When a regular poll is pending, the worker MUST finish its
current request but MUST NOT start another background item before the poll has
acquired the request gate.

Initial backfill ordering MUST be:

1. current JST month before previous JST month;
2. newest missing 7-day window before older windows within each month; and
3. deterministic supply-point HMAC and direction ordering for equal priority.

The initial background target ends at the first successful bootstrap end minus
72 hours. The regular poll owns the final 72 hours. Daily reconciliation covers
the complete previous and current JST month through its execution time and is
queued once per JST calendar day for each queryable direction.

A background item MUST reconcile its authoritative direction result into the
ledger and publish a new coordinator snapshot without triggering another
network poll. Ledger mutation, checkpoint mutation, aggregation, and snapshot
publication MUST be serialized by a coordinator-owned lock.

## 6. Persistent backfill checkpoints

Each initialized supply point MUST have a versioned private Home Assistant Store
with a name equivalent to:

```text
octopus_energy_japan.sync.<entry_id>.<supply-point-hmac>
```

The filename and JSON keys MUST NOT contain a raw account number, supply-point
identifier, SPIN, meter identifier, register identifier, address, or customer
name.

Schema version 1 MUST persist:

- the JST month-pair generation, for example `2026-06/2026-07`;
- successfully completed initial-backfill windows keyed by direction;
- the last fully completed daily-reconciliation JST date keyed by direction;
  and
- the checkpoint schema version.

A successful authoritative empty direction response counts as a completed
window. A failed direction or partially parsed target/page response does not.
Import completion MUST NOT mark export complete, and export completion MUST NOT
mark import complete.

For a background item, durable write order MUST be:

1. reconcile the complete authoritative direction result in memory;
2. flush every affected ledger partition;
3. persist completion for all current item obligations and that direction; and
4. publish the new immutable coordinator snapshot.

The checkpoint MUST never be persisted ahead of the ledger. A crash between
steps 2 and 3 may cause a harmless idempotent refetch; a crash MUST NOT cause a
checkpoint to suppress data that was never durably written.

On restart, the queue MUST be reconstructed from the month generation, planner,
queryable directions, and completed checkpoints. A month-generation change
removes obsolete initial obligations only; it MUST NOT delete ledger data or a
request scope still required by another obligation. Lifecycle disablement
retains both ledger and checkpoint stores.

When a new direction becomes queryable, runtime MUST enqueue its missing
initial windows even if another direction for the same point is already fully
backfilled.

## 7. Regular refresh algorithm

Each 30-minute coordinator refresh MUST:

1. refresh discovery when the 24-hour cadence is due;
2. reconcile enabled, disabled, historical, missing, and newly discovered
   resources according to section 8;
3. initialize stores for newly enabled points;
4. attempt the latest 72-hour window for every candidate direction of every
   enabled point;
5. reconcile each successful authoritative direction result;
6. update in-process direction coverage for every successful query window,
   including an empty result;
7. aggregate only enabled points and successful queryable directions from the
   ledger;
8. publish one immutable coordinator snapshot; and
9. enqueue missing initial or due daily obligations without waiting for them.

Authentication failure is entry-wide and MUST abort immediately. Other failures
MUST be isolated at the narrowest safe scope:

- a direction-specific authorization or schema failure affects that direction;
- a supply-point-specific invalid response affects that supply point;
- the first rate-limit or shared transport failure stops additional requests in
  that refresh to prevent a request storm; and
- already reconciled successful directions remain committed.

If at least one enabled direction succeeds, the coordinator MUST publish a
partial snapshot with explicit failed or stale point/direction status. If all
enabled directions fail, `UpdateFailed` MUST preserve the previous coordinator
data. A partial failure MUST NOT make unrelated successful supply-point entities
unavailable.

The coordinator snapshot MUST contain enough typed state to determine, without
consulting mutable internals:

- discovered accounts and capabilities;
- enabled and currently present supply points;
- per-point and per-direction last success and current failure/stale status;
- per-point queryable directions;
- per-direction period query coverage;
- provider and fallback observation per supply point and direction;
- immutable calendar aggregates;
- correction and last-refresh change counts; and
- corrupt-partition count.

## 8. Resource lifecycle state machine

Resource selection MUST be recalculated after every successful discovery using
these exact rules:

- an active or unknown account is enabled automatically;
- under an active or unknown account, active/unknown points are enabled
  automatically and historical points require explicit point selection;
- an unselected historical account disables every child point and ignores any
  stale child selection;
- selecting a historical account enables all of its discovered child points;
  separate child selection under that account is unnecessary; and
- a point that disappears is never considered enabled merely because an old
  selection remains in options.

Required transitions are:

| Transition | Required behavior |
|---|---|
| New active or unknown point | Enable automatically, create/reuse stores, run 72-hour bootstrap, create status entity, then create direction entities after successful direction selection |
| Historical point under active account explicitly selected | Keep enabled and synchronize normally |
| Historical account explicitly selected | Enable the account and all discovered child points |
| Active/unknown to historical, not selected | Stop network synchronization, exclude its ledger from active aggregation, integration-disable its device, retain stores and registry entries, mark existing entities unavailable |
| Point disappears from discovery | Stop network synchronization, exclude it from aggregation, retain stores and registry entries, mark existing entities unavailable, do not delete the device |
| Missing or historical point becomes active again | Reuse the same HMAC device, entity unique IDs, ledger, and checkpoints; resume synchronization |
| Historical selection removed by reconfigure | Reload the entry and apply the same disabled behavior without deleting history |

The set used to read ledger records for aggregation MUST be exactly the set of
enabled supply-point runtime states. Iterating every previously initialized
state is forbidden because it would continue projecting deselected or closed
history.

Device projection may create registry devices for disabled historical resources
so that the user can reconfigure them. Sensor platforms MUST NOT create new
energy entities for a resource that has never been enabled. Previously created
entities are retained and become unavailable rather than being removed.

## 9. Direction and provider topology

Direction selection is per supply point and per direction. Global schema
capability alone MUST NOT create an entity.

The provider layer MUST support mixed selection for one supply point, for
example generic import with unsupported export, without discarding the
successful direction. Provider selection MUST therefore be represented per
direction rather than by one provider field for the whole supply point.

For each generic direction:

1. query all configured targets and all pages;
2. treat the direction as successful only if every required target/page
   succeeds;
3. retry without optional quality fields only for the existing allow-listed
   quality-permission case;
4. treat a successful empty result as a queryable direction; and
5. return its complete authoritative series set and observation timestamp.

An allow-listed generic permission, disabled-field, or recognized compatibility
failure MAY fall back only that failed direction. Legacy fallback is permitted
only if the legacy provider can represent the same direction. Authentication,
rate limit, timeout, server, malformed-response, and identifier errors MUST NOT
fall back.

Legacy direction is the explicit normalized supply-point direction. When legacy
discovery provides no direction, it is import only. Legacy capability MUST NOT
create export entities by inference.

After a successful direction result, queryable directions MUST be derived from
its authoritative series, not from raw reading presence. This permits a valid
export entity with zero readings while preventing an unsupported export entity.

Entity creation rules are:

- status entity: after discovery for an enabled point;
- directional energy entities: after the first successful authoritative result
  for that direction;
- newly successful direction: add entities exactly once and enqueue its missing
  background history;
- direction no longer queryable: retain existing entities but mark them
  unavailable; and
- never delete or recreate an entity merely because provider selection changed.

## 10. Error classification and retry contract

The HTTP and GraphQL layers MUST preserve structured retry information.
`Retry-After` MUST support delta-seconds and HTTP-date forms, be clamped to zero
through one hour, and be ignored when malformed.

Required HTTP classification is:

| Condition | Exception class/behavior |
|---|---|
| HTTP 401 | authentication |
| HTTP 403 | authorization |
| HTTP 429 | rate limit with optional `Retry-After` |
| HTTP 408, 425, 500, 502, 503, 504 | transient HTTP/transport failure with optional `Retry-After` |
| Other non-success HTTP status | non-retriable typed HTTP failure |
| Network or timeout | transient transport failure |
| GraphQL rate-limit code | rate limit, preserving any response-header `Retry-After` |
| GraphQL authentication/authorization/validation | existing typed GraphQL exceptions |
| Malformed payload | invalid response, non-retriable |

Setup and regular poll MUST NOT sleep internally for retry. Home Assistant owns
setup retry timing, and the next coordinator cycle owns regular-poll retry.

Only background work retries internally:

- retriable: rate limit, timeout, network failure, and classified transient HTTP
  status;
- non-retriable: authentication, authorization, validation/schema,
  not-found/identifier, malformed response, and ledger invariant failure;
- rate limit sets an entry-wide background `not_before` time;
- other transient errors delay only the failed item;
- retry delay uses provider `Retry-After` when present, otherwise deterministic
  full-jitter exponential backoff with a 30-second base and 1-hour maximum;
- waiting MUST release the request gate and ledger lock;
- after five transient failures, reset the activation attempt counter and defer
  that item for six hours; and
- authentication failure stops the worker and initiates reauthentication.

A non-retriable background failure remains failed for its current obligation
generation. It may be reconsidered only after a new discovery/capability
snapshot, a new JST daily-reconciliation generation, or explicit user retry in
a later Repairs phase. It MUST NOT spin or block unrelated obligations.

## 11. Coverage and entity semantics

Successful query windows, not the presence of individual readings, define
runtime coverage. Coverage ranges are per supply point and direction, are
half-open UTC intervals, and MUST merge adjacent or overlapping ranges.

Entity source of truth is:

| Entity | Source and availability rule |
|---|---|
| Latest reported interval energy | latest completed ledger interval for the direction; no full-period coverage requirement |
| Latest reading timestamp | latest completed provider interval end for the direction |
| Data delay | coordinator time minus latest completed interval end |
| Data available | true only when at least one completed interval exists for the direction |
| Today/yesterday/week/month/last month | direction-specific ledger calendar aggregate; state is unknown until that direction's query coverage spans the complete requested calendar window through projection time |
| Supply-point status | normalized discovery lifecycle; independent of reading availability |

A successful authoritative query may still contain delayed or missing provider
intervals. The integration reports only what OEJP supplied and uses latest
reading/data delay to expose staleness. It MUST NOT synthesize zero-energy
intervals.

The integration MUST avoid the terms “live”, “real-time”, and “current power”.
Period sensors are convenience projections and MUST NOT be used as the Energy
Dashboard source of truth. PR 8 projects external statistics directly from the
ledger.

Entity and device identifiers MUST use installation-local HMACs. Raw account
numbers, supply-point identifiers, SPINs, meter/register identifiers, addresses,
names, OAuth values, and raw reading arrays MUST NOT appear in entity states,
attributes, unique IDs, device names, logs, translations, or diagnostics.

## 12. Shutdown and unload

Runtime shutdown MUST be idempotent.

`async_unload_entry` MUST:

1. set a closing flag so no new queue item or refresh begins;
2. cancel and await the background worker;
3. unload platforms;
4. if platform unload fails, clear the closing flag, restart the worker, and
   return `False`;
5. flush every initialized ledger and checkpoint store;
6. release listeners and task references; and
7. clear `entry.runtime_data`.

Cancellation MUST not be converted to `UpdateFailed`. A request or write that is
already inside an atomic persistence operation MUST be allowed to finish or be
safely repeated after restart.

## 13. Required regression tests

Tests MUST use the Home Assistant harness where lifecycle behavior is involved
and synthetic sanitized fixtures for provider behavior. The following behaviors
are mandatory:

- first refresh performs only 72-hour poll work and starts no blocking backfill;
- setup cleanup after partial ledger initialization;
- one request in flight per config entry, including generic topology discovery;
- current-month then previous-month newest-first queue ordering;
- request-scope deduplication across initial and daily generations, obligation
  coalescing, priority upgrade, and obsolete-obligation removal;
- direction-specific checkpoints and no cross-direction completion leakage;
- checkpoint write occurs only after ledger flush;
- restart reconstructs only missing initial direction windows;
- successful empty direction response completes coverage;
- regular poll preempts the next background item;
- bounded rate-limit retry, `Retry-After`, jitter fallback, five-attempt defer,
  and non-retriable no-spin behavior;
- multiple accounts and multiple supply points across multiple windows;
- partial point/direction success publishes successful results without exposing
  failed directions as healthy;
- 24-hour discovery refresh;
- active-to-historical, disappearance, reappearance, and reconfigure deselection;
- historical-account selection semantics and stale child-selection rejection;
- disabled states are excluded from aggregation while ledger data is retained;
- generic import success with export failure does not discard import;
- successful empty generic export creates an export direction;
- legacy unknown direction creates import only;
- newly queryable direction schedules its own missing backfill;
- dynamic point/direction entity addition has no duplicates;
- period sensors remain unknown until their own direction coverage is complete;
- unload/reload preserves HMAC identities, ledger data, and checkpoints;
- authentication versus transient versus non-retriable error mapping;
- English/Japanese translation completeness; and
- no raw provider identifier is exposed by device/entity representations.

The complete repository MUST pass Ruff, Ruff format, strict mypy, the full pytest
suite, the 95% line and branch gate, Hassfest, HACS validation, link validation,
Security, CodeQL, and dependency review.

## 14. PR 7 definition of done

PR 7 is complete only when:

1. every requirement in this document is implemented;
2. the code contains no alternate runtime path that violates this document;
3. all mandatory regression tests exist and pass;
4. `MASTER_TECHNICAL_DESIGN_V3.md`,
   `LEDGER_AND_AGGREGATION.md`, code comments, and the PR body contain no
   contradictory runtime statement;
5. no unresolved TODO, FIXME, placeholder, open design choice, or unchecked
   completion item remains in PR 7 scope;
6. all required GitHub checks pass on the final head; and
7. the PR is marked ready for review but is not merged automatically.

PR 8 Energy statistics MUST NOT begin before this definition of done is met.
