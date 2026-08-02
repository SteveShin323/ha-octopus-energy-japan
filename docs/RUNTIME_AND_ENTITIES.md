# Runtime execution and entity projection specification

Status: normative implementation contract for Full Development Plan v3 PR 7
Reviewed: 2026-08-02

This document defines the only permitted runtime design for the Home Assistant
integration. It is subordinate to
[`MASTER_TECHNICAL_DESIGN_V3.md`](MASTER_TECHNICAL_DESIGN_V3.md), implements
[ADR 0004](adr/0004-non-blocking-runtime-synchronization.md), and is the
controlling specification for PR 7 where it is more specific.

The terms **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative. PR 7 is
not complete while any requirement here is unimplemented, untested, or
contradicted by code or another non-archived document.

## 1. Scope and authority

PR 7 owns:

- config-entry runtime setup, refresh, reload, and unload;
- bounded reading synchronization;
- initial and daily historical reconciliation execution;
- discovery-driven lifecycle transitions;
- persistent direction-specific background-work checkpoints;
- authoritative query-coverage tracking;
- per-supply-point and per-direction provider observations and failures;
- immutable coordinator snapshots;
- account and supply-point devices;
- consumption, timestamp, delay, status, and data-availability entities; and
- English and Japanese entity translations.

PR 7 does not implement external recorder statistics, tariff or billing
entities, diagnostics downloads, Repairs issues, or release packaging. Later
phases MUST consume these runtime and ledger contracts rather than replace them.

[`PR7_DELIVERY_PLAN.md`](PR7_DELIVERY_PLAN.md) controls branch and child-PR
mechanics. It does not alter runtime behavior defined here.

## 2. Required runtime ownership

One OAuth login-scoped config entry MUST own exactly one of each:

- shared `AuthSession`;
- shared authenticated GraphQL client;
- request gate permitting at most one logical GraphQL operation in flight;
- latest typed discovery and capability snapshot;
- `OejpDataUpdateCoordinator`;
- one background `OejpSyncQueue` worker;
- one persistent interval ledger per initialized supply point; and
- one private versioned sync-checkpoint store per initialized supply point.

The pure window planner remains in `sync.py`. Network execution, request
priority, retry state, queue mutation, coverage mutation, and checkpoint
persistence MUST be isolated in `sync_runtime.py` or an equivalently focused
module.

Entities consume immutable coordinator data only. They MUST NOT call OEJP,
parse GraphQL dictionaries, retain OAuth tokens, mutate ledgers or checkpoints,
or independently infer provider support.

## 3. Fixed constants

These values MUST be used unless a later accepted ADR changes them:

| Constant | Required value |
|---|---:|
| Regular poll interval | 30 minutes |
| Regular poll overlap | most recent 72 hours |
| Maximum reading query window | 7 days |
| Discovery interval | 24 hours |
| Blocking setup backfill | none |
| Initial background backfill | previous and current JST month, excluding the regular 72-hour window |
| Daily background reconciliation | previous and current JST month |
| Maximum GraphQL concurrency per config entry | 1 |
| GraphQL request timeout | 30 seconds |
| Background-start stagger | deterministic 0 to 5 minutes |
| Retry base | 30 seconds |
| Retry maximum | 1 hour |
| Attempts per background activation | 5 |
| Deferred retry after five transient failures | 6 hours |
| Optional long backfill limit | 13 months |

Startup staggering applies only before the first background item. It MUST NOT
delay setup, the first refresh, or a regular poll.

## 4. Request gate and transport boundary

The gate MUST wrap the complete logical authenticated GraphQL operation, from
authorization-header acquisition through the existing single token-refresh retry.
The permit MUST remain held across that refresh-and-retry sequence so two
callers cannot issue overlapping GraphQL attempts after simultaneous token
expiry.

Core discovery, schema capability detection, generic topology discovery,
reading pages, and later optional operations made by one config entry MUST all
use that same gated client. Direct access to the ungated transport from runtime
code is forbidden.

No unbounded `asyncio.gather` is permitted for GraphQL work. Generic targets,
directions, and pagination MAY be planned together, but network calls MUST pass
through the gate and therefore execute with one in-flight request per entry.

The gate alone is not a priority scheduler. The background worker MUST check an
explicit poll-pending condition before acquiring it, as specified in section 8.

## 5. Config-entry setup contract

`async_setup_entry` MUST perform these steps in order:

1. resolve the Home Assistant OAuth implementation and OEJP metadata;
2. construct the shared auth session and obtain a valid authorization header;
3. construct the gated authenticated GraphQL client;
4. perform strict core discovery and optional capability/topology discovery;
5. load the installation-local identity secret;
6. construct runtime data, coordinator, ledgers as needed, queue, and checkpoint
   stores;
7. run the first coordinator refresh using only the regular 72-hour window;
8. project account and supply-point devices;
9. forward sensor and binary-sensor platforms; and
10. start the background worker.

The first refresh MUST NOT call the initial-month planner, run daily
reconciliation, run optional long backfill, or sleep for staggering or retry.

Setup outcomes are exact:

- authentication failure raises `ConfigEntryAuthFailed`;
- transient or rate-limit failure of strict discovery raises
  `ConfigEntryNotReady`;
- no enabled supply points succeeds with an empty reading snapshot;
- at least one successful authoritative direction succeeds with partial status
  for other points or directions;
- zero successful directions plus at least one shared transient/rate-limit
  failure raises `ConfigEntryNotReady`; and
- zero successful directions where every result is a permanent
  direction-specific unsupported/forbidden/validation outcome succeeds with
  status devices only and no directional entities, avoiding an endless setup
  retry loop.

Any failure after runtime allocation MUST cancel and await created tasks, flush
pending ledger and checkpoint writes, release listeners and references, clear
`entry.runtime_data`, and avoid forwarding platforms. Home Assistant `Store`
objects do not require an invented close operation. Cleanup MUST be idempotent.

## 6. Candidate directions and provider topology

A candidate direction is permission to probe, not permission to create an
entity. For each enabled supply point, the candidate set is the deterministic
union of:

1. directions that were previously queryable for that point;
2. an explicit normalized discovery direction when it is import or export;
3. generic import/export fields currently reported as supported by capability
   discovery; and
4. import as the legacy probe direction when discovery direction is unknown and
   at least one legacy reading capability is not definitively unsupported or
   forbidden.

If capability introspection is unavailable, previously queryable and explicit
legacy directions MUST continue to be attempted. Global schema capability alone
MUST never create an entity.

The provider contract MUST return one complete authoritative result per supply
point and direction. A batch-wide provider decision spanning import and export
is forbidden.

For each generic direction:

1. query every configured target and every page;
2. succeed only if every required target/page succeeds;
3. retry without optional quality fields only for the existing allow-listed
   quality-permission case;
4. treat a successful empty response as authoritative and queryable; and
5. return the full authoritative-series set and one observation timestamp.

Allow-listed generic permission, disabled-field, or recognized compatibility
failure MAY fall back only that direction. Legacy fallback is allowed only when
legacy can represent the same direction. Authentication, rate limit, timeout,
server, malformed-response, and identifier errors MUST NOT fall back.

Legacy uses the explicit normalized direction. Unknown legacy direction is
import only. Legacy capability MUST NOT infer export.

A direction becomes queryable only after a successful authoritative result,
including an empty one. A transient failure keeps a previously queryable
direction but marks it stale/unavailable for that refresh. A definitive
unsupported outcome for every eligible provider marks it non-queryable for the
current discovery/capability generation. A later successful discovery or probe
may restore it without changing entity identity.

## 7. Regular refresh and partial failure

Each 30-minute refresh MUST:

1. refresh discovery when the 24-hour cadence is due;
2. apply the lifecycle state machine in section 10;
3. initialize stores for newly enabled points;
4. set the poll-pending condition;
5. attempt the latest 72-hour window for each candidate direction in
   deterministic point/direction order;
6. reconcile every successful authoritative direction;
7. update in-process recent coverage, including successful empty windows;
8. aggregate only enabled points and currently queryable directions;
9. publish one immutable snapshot; and
10. enqueue missing initial or due daily obligations without waiting for them.

Authentication failure is entry-wide and aborts immediately. Other failures are
isolated at the narrowest safe scope:

- direction-specific authorization, compatibility, or validation affects that
  direction;
- point-specific malformed or identifier response affects that point;
- the first rate-limit or shared transport failure stops further requests in
  that refresh to avoid a storm; and
- already reconciled successes remain committed.

If at least one enabled direction succeeds, publish a partial snapshot. If all
attempted directions fail transiently, raise `UpdateFailed` and preserve the
previous coordinator data. Permanent-only direction failures publish a snapshot
with explicit failure state rather than retrying the whole coordinator forever.
Unrelated successful entities MUST remain healthy.

The immutable snapshot MUST contain:

- discovered accounts and capabilities;
- present and enabled supply points;
- candidate and queryable directions per point;
- per-point/direction last success, stale state, and current safe error class;
- authoritative coverage ranges per direction;
- provider/fallback observation per direction;
- calendar aggregates;
- correction and last-refresh change counts; and
- corrupt-partition count.

## 8. Background work and priority

A queue item identifies one request scope:

```text
supply-point HMAC
+ import/export direction
+ half-open UTC start/end window of at most 7 days
```

It carries one or more obligations:

```text
sync reason + generation identifier
```

Priorities are:

| Priority | Reason |
|---:|---|
| 0 | regular poll; coordinator-owned, not queue work |
| 10 | daily reconciliation |
| 20 | initial current-month backfill |
| 30 | initial previous-month backfill |
| 40 | optional long backfill |

The deduplication key is point, direction, start, and end; reason and generation
are excluded. Enqueuing the same scope adds an obligation and upgrades effective
priority to the highest-priority obligation. One authoritative success satisfies
all current obligations on that item.

Only one worker exists per entry. Before starting each item it MUST check the
poll-pending condition. If a poll is pending, the worker finishes no new request
until the poll acquires and releases the gate. An already running background
request is not cancelled solely for preemption.

Initial ordering is current month before previous month, newest missing window
before older windows, then deterministic point-HMAC and direction order.
The final 72 hours are owned by regular polling and excluded from initial work.

Daily generation identifiers MUST include the JST date and exact UTC target end.
A daily generation has a per-direction completion barrier covering every planned
window. A newer daily generation supersedes older incomplete daily obligations
that are outside the current previous/current-month target; it MUST NOT remove a
scope still required by initial or optional obligations.

Background reconciliation MUST update the ledger and publish a new immutable
snapshot without triggering a network poll. Ledger mutation, coverage mutation,
checkpoint mutation, aggregation, and snapshot publication MUST be serialized
by a coordinator-owned lock. Network waiting MUST occur outside that lock.

## 9. Checkpoints, durability, and coverage reconstruction

Each initialized supply point has one private versioned store named equivalently
to:

```text
octopus_energy_japan.sync.<entry_id>.<supply-point-hmac>
```

Filenames and JSON keys MUST NOT include raw account, supply-point, SPIN,
meter/register, address, name, or OAuth data.

Schema version 1 MUST persist:

- schema version;
- current JST month-pair generation;
- generation metadata and exact target end for each active reason;
- completed windows keyed by direction, reason, and generation;
- merged background authoritative coverage ranges keyed by direction; and
- the last fully completed daily JST date and completed-through UTC timestamp
  keyed by direction.

A daily completed date advances only after every window in that direction's
generation has passed the durability sequence below. Partial daily window
completion MUST be persisted so restart does not needlessly replay successful
work. Import completion never marks export complete or vice versa.

For every successful background direction window, durability order is:

1. reconcile the complete authoritative result in memory;
2. flush every affected ledger partition;
3. persist completed obligations and merged background coverage;
4. if a daily barrier is now complete, persist its completed date/through value;
   and
5. publish the immutable coordinator snapshot.

A checkpoint MUST never precede ledger durability. A crash between ledger flush
and checkpoint save causes an idempotent refetch, never lost data.

A successful authoritative empty response completes the window and coverage. A
failed or partially parsed target/page response completes neither.

On restart, reconstruct queue work from planner generations, candidate/queryable
directions, and completed windows. Reconstruct historical coverage from
persisted background coverage, then add the successful first 72-hour poll as
recent in-process coverage. Regular-poll coverage need not be written every 30
minutes because setup always performs a new first poll before entities are
forwarded.

A month-generation change removes only obsolete obligations/checkpoints; it
MUST NOT delete ledger data or another reason's current obligation. Lifecycle
disablement retains ledger and checkpoint stores. A newly queryable direction
enqueues its own missing windows independently.

## 10. Resource lifecycle state machine

Selection is recalculated after every successful discovery:

- active/unknown accounts are enabled automatically;
- under them, active/unknown points are enabled automatically and historical
  points require explicit point selection;
- an unselected historical account disables every child and ignores stale child
  selections;
- selecting a historical account enables all discovered child points; and
- a missing point is never enabled by stale options.

Required transitions:

| Transition | Required behavior |
|---|---|
| New active/unknown point | Enable, create/reuse stores, probe 72 hours, create status entity, then create directional entities only after successful direction results |
| Selected historical point under active account | Synchronize normally |
| Selected historical account | Enable all discovered child points |
| Active/unknown to unselected historical | Stop requests, cancel its pending obligations, exclude ledger from aggregation, integration-disable device, retain stores/registry, mark existing entities unavailable |
| Point disappears | Same as disablement, without deleting device or history |
| Missing/historical becomes active | Reuse HMAC device, entity IDs, ledger, coverage, and checkpoints; resume requests |
| Historical selection removed | Reload and apply the same disabled behavior |

Aggregation MUST iterate exactly enabled runtime states, never all initialized
states. Device projection MAY register disabled historical resources for
reconfiguration. Sensor platforms MUST NOT create new energy entities for a
resource never enabled. Existing entities are retained rather than deleted.

For an enabled present point, the status entity is available independently of
reading success. When the point becomes disabled or missing, all existing
entities, including status, become unavailable and the integration-managed
device is disabled as applicable.

## 11. Error and retry contract

Transport and GraphQL exceptions MUST preserve a safe typed category, HTTP
status where relevant, and optional `retry_after`. `Retry-After` supports
seconds and HTTP-date, is clamped to zero through one hour, and is ignored when
malformed.

| Condition | Required classification |
|---|---|
| HTTP 401 | authentication |
| HTTP 403 | authorization |
| HTTP 429 | rate limit with optional retry-after |
| HTTP 408, 425, 500, 502, 503, 504 | transient HTTP failure with optional retry-after |
| Other non-success HTTP | non-retriable typed HTTP failure |
| Network or timeout | transient transport failure |
| GraphQL rate-limit code | rate limit, preserving response-header retry-after |
| GraphQL authentication/authorization/validation | existing typed GraphQL class |
| Malformed payload | invalid response, non-retriable |

Setup and regular poll MUST NOT sleep for retry. Home Assistant owns setup retry;
the next coordinator cycle owns poll retry.

Only background work retries internally:

- rate limit, timeout, network, and classified transient HTTP are retriable;
- authentication, authorization, validation/schema, not-found/identifier,
  malformed response, and ledger invariant failures are non-retriable;
- rate limit sets an entry-wide background `not_before`;
- other transient failures delay only the item;
- delay uses valid provider retry-after or deterministic full-jitter exponential
  backoff with 30-second base and 1-hour maximum;
- waiting releases request gate and ledger lock;
- five transient failures reset the activation counter and defer six hours;
- authentication stops the worker and initiates reauthentication; and
- permanent failure remains failed for that obligation generation and cannot
  spin or block unrelated work.

A permanent direction failure may be reconsidered only on a new relevant
capability/discovery generation, a new daily generation, or later explicit user
retry.

## 12. Coverage and entity semantics

Coverage is successful authoritative query ranges, not reading presence. It is
per point/direction, half-open UTC, and merges adjacent or overlapping ranges.

| Entity | Source and availability rule |
|---|---|
| Latest reported interval energy | latest completed ledger interval; no full-period coverage requirement |
| Latest reading timestamp | latest completed provider interval end |
| Data delay | coordinator time minus latest completed interval end |
| Data available | true only when at least one completed interval exists |
| Today/yesterday/week/month/last month | ledger calendar sum; state unknown until that direction's coverage spans the complete requested JST period through projection time |
| Supply-point status | normalized lifecycle for an enabled, present point |

After complete authoritative coverage, a period sum is the sum of records OEJP
supplied. A completely empty authoritative range may therefore project zero,
while `data_available` remains false. The integration MUST NOT fabricate
individual zero-energy intervals.

A current direction failure makes that direction's entities unavailable while
unrelated directions remain available. A later success restores them without
changing unique IDs.

Directional entities are created only after the first successful authoritative
result. A successful empty export result creates export entities. New point or
direction entities are submitted exactly once. Provider switching never deletes
or recreates them.

The integration avoids “live”, “real-time”, and “current power”. Period sensors
are convenience projections, not Energy Dashboard truth. PR 8 projects external
statistics directly from the ledger.

All entity/device identifiers use installation-local HMACs. Raw provider IDs,
SPINs, meter/register IDs, addresses, names, OAuth values, and raw readings MUST
NOT appear in states, attributes, unique IDs, device names, logs, translations,
or diagnostics.

## 13. Shutdown and unload

Shutdown MUST be idempotent.

`async_unload_entry` MUST:

1. set a closing flag preventing new refreshes or queue mutations;
2. let any atomic persistence section finish, then cancel and await the worker;
3. unload platforms;
4. if unload fails, clear closing, reconstruct/restart one worker, and return
   `False` without clearing runtime data;
5. flush every initialized ledger and checkpoint store;
6. release listeners and task references; and
7. clear `entry.runtime_data`.

Cancellation MUST propagate as cancellation, not `UpdateFailed`. An interrupted
request or pre-checkpoint write must be safely repeatable after restart.

## 14. Mandatory regression matrix

Lifecycle tests use the Home Assistant harness; provider tests use sanitized
synthetic fixtures. Tests MUST cover:

- first refresh performs only 72-hour work;
- permanent-only no-direction setup succeeds status-only, while all-transient
  setup fails retryably;
- cleanup after partial ledger initialization;
- the gate spans auth refresh/retry and permits one logical operation in flight,
  including topology discovery;
- exact candidate-direction rules and no capability-only entity creation;
- per-direction generic results, mixed import success/export failure, and
  direction-only fallback;
- successful empty generic export creates a queryable export direction;
- legacy unknown direction probes import only;
- current-month then previous-month newest-first ordering;
- obligation coalescing, priority upgrade, daily supersession, and obsolete
  obligation removal;
- direction/reason/generation checkpoint separation;
- partial daily completion, completion barrier, and completed-through timestamp;
- checkpoint save occurs after ledger flush;
- restart reconstructs missing work and historical coverage exactly;
- newly queryable direction schedules independent history;
- regular poll preempts the next background request;
- retry-after seconds/date parsing, jitter fallback, five-attempt defer,
  entry-wide rate-limit not-before, and permanent no-spin behavior;
- multiple accounts/points/directions across multiple windows;
- partial success publishes successful results without marking failed directions
  healthy;
- 24-hour discovery refresh;
- active-to-historical, disappearance, reappearance, and reconfigure deselection;
- historical-account selection and stale-child rejection;
- disabled states excluded from aggregation with stores retained;
- dynamic entity addition without duplicates;
- period sensors unknown until reconstructed direction coverage is complete;
- unload failure restarts exactly one worker;
- unload/reload preserves HMAC IDs, ledger, checkpoints, and coverage;
- authentication/transient/permanent mapping;
- English/Japanese translation completeness; and
- no raw provider identifier exposure.

The repository MUST pass Ruff, Ruff format, strict mypy, full pytest, 95% line
and branch coverage, Hassfest, HACS, links, Security, CodeQL, Codecov, and
dependency review.

## 15. Definition of done

PR 7 is complete only when:

1. every requirement here is implemented;
2. no alternate runtime path violates it;
3. all mandatory regression tests exist and pass;
4. the master design, ledger document, ADRs, delivery plan, comments, and PR body
   contain no contradictory runtime statement;
5. no PR 7 TODO, FIXME, placeholder, open design choice, temporary compatibility
   path, or unchecked completion item remains;
6. all required checks pass on the final `codex/runtime-entities` head; and
7. PR #14 is marked ready for review but is not merged automatically.

PR 8 Energy statistics MUST NOT begin before this definition is met.
