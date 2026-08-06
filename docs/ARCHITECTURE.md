# Architecture

Where to look in the code, and the rules that must hold. The code and its tests define the
behaviour; this page only says where things are and why.

## Words used here

The same thing is called the same thing throughout. Each term matches an identifier in the
code, so it can be grepped.

| Term | Means |
|---|---|
| **the API** | Octopus Energy Japan's GraphQL API. Not called "the provider", which would collide with the two reading providers below |
| **OEJP** | Octopus Energy Japan. Used in class and constant names — `OejpError`, `OEJP_AUTH_ISSUER` — and in the decision records, not in prose here |
| **reading provider** | `GenericReadingsProvider` or `LegacyHalfHourlyProvider`, the two internal classes that fetch intervals |
| **interval** | one 30-minute reading for one supply point and direction |
| **correction** | an interval the API re-publishes with a new `version`, whose value may differ |
| **the poll** | `_async_update_data`, which runs every 30 minutes |
| **the background worker** | `_async_background_worker`, which drains queued windows behind the poll |
| **window** | a start and end time one request covers |
| **capability** | a field or query discovery confirmed this account can use, held in `CapabilitySnapshot` |
| **period** | the span a stepped tariff's cumulative kWh accumulates over, from `billing_period.py`. Anchored on the reported meter-reading day, or the Asia/Tokyo calendar month when none is reported |

## Where the code lives

Grouped by responsibility. This is not a strict layering — see the dependency rule below.

| Group | Modules | Responsibility |
|---|---|---|
| Authentication | `oauth.py`, `oauth_metadata.py`, `password_auth.py`, `application_credentials.py`, `api/device_auth.py` | obtain and renew a bearer token |
| Transport | `api/client.py`, `api/auth.py`, `api/errors.py` | one GraphQL POST, error classification, retries |
| Operations | `api/discovery.py`, `api/readings.py`, `api/commercial.py`, `api/tariff.py`, `api/rate_limit.py`, `api/operations.py` | query documents and parsers, returning typed models |
| Ledger | `ledger.py`, `ledger_store.py` | store every interval, keyed so a correction replaces it |
| Tariff history | `tariff_history.py`, `tariff_history_store.py` | archive the rate adjustments the API stops serving |
| Aggregation | `aggregation.py` | Asia/Tokyo calendar totals over the ledger |
| Statistics | `statistics.py`, `statistics_runtime.py`, `tariff_cost.py`, `billing_period.py` | external long-term statistics, energy and cost |
| Coordination | `coordinator.py`, `commercial_coordinator.py`, `sync.py`, `sync_runtime.py`, `sync_store.py`, `background_sync.py` | the poll, the background worker, checkpoints |
| Presentation | `sensor.py`, `binary_sensor.py`, `button.py`, `entity.py`, `runtime.py`, `diagnostics.py`, `issues.py` | entities, devices, diagnostics, repair issues |

**The dependency rule that does hold:** nothing under `api/` imports Home Assistant or any
module outside `api/`. That package is a standalone client, testable without Home Assistant.
Everything else may import anything above it in the table, and `coordinator.py` also imports
two helpers from `runtime.py`, so the ordering is a reading order rather than a strict
layering.

`identity.py` is used by every group. Devices, entities, and statistics are addressed by an
HMAC of an installation-local secret and the API's identifier, so no raw identifier reaches
Home Assistant's registries.

## Authentication

Three methods. The config flow records the choice as `auth_method` on the config entry, and
`__init__.py` routes on it, refusing a value it does not recognise.

| Method | Flow | Needs |
|---|---|---|
| Password | `obtainKrakenToken` with email and password | nothing |
| OAuth authorization code | public client, PKCE S256 | a published client ID |
| Device authorization grant | public client, RFC 8628 | a published client ID |

The password method stores the credential. The API's refresh token lasts seven days and
renewing it does not extend that expiry, so after seven days nothing but the credential can
sign in again. A rejected renewal falls back to a full sign-in; a rejected sign-in is final
and raises reauthentication.

One login owns one config entry. Changing method upgrades that entry in place: its ledger and
statistics are kept, and any stored password is deleted. Deleting the entry removes its stored
data, including the installation secret if it is the last entry.

## Reading providers

The **generic** provider calls `readings` at the most granular level discovery found —
register, else device, else supply point — so the same energy is never counted twice at two
levels. Import and export are separate connections.

The **legacy** provider calls `halfHourlyReadings` by account and time range.

Falling back from generic to legacy is allowed for exactly four causes:

1. discovery observed the capability as unsupported or forbidden;
2. an authorization error whose GraphQL path is confined to a reading field;
3. a disabled-field error (`KT-CT-1113`); or
4. a generic series that is null after the supply point itself was found.

Everything else stays visible: authentication failures, rate limits, transport errors,
malformed responses, and validation errors the code does not recognise. Adding a fifth cause
would let a real fault look like a capability gap, and the wrong provider would be used
silently.

## Ledger and aggregation

**Every interval is stored, not a running total.** When a billing period closes, the API
re-publishes its intervals with a new `version` and the values can change. A running total
cannot be corrected. An interval's key covers the series it belongs to — account, supply
point, direction, unit, source, and device or register — plus its start and end. The key
deliberately excludes `version`, so a re-published interval replaces the earlier one in place
instead of being stored beside it.

Storage is split into one partition per month. A partition that fails to load is skipped
rather than failing setup, and a repair issue reports which one.

Calendar totals use **Asia/Tokyo** day, week, and month boundaries. A period reports `unknown`
until every interval in it has arrived, so a half-synchronised day is never shown as a
complete day with a low number. These boundaries do not match a billing period, which ends at
a meter read a few hours after midnight.

"Every interval has arrived" is decided by asking whether authoritative coverage reaches the
snapshot's own timestamp, which makes a running period — today, this week, this month —
sensitive to how that snapshot is dated. A poll satisfies it by construction: the instant it
plans its windows around is the instant it dates the snapshot with. **A snapshot built
anywhere else keeps the last poll's date**, because it has read nothing newer; dating it with
the wall clock would claim an instant no window covers and empty every running period until
the next poll.

One response is capped at 1488 intervals and silently drops the oldest beyond that, so
requests use seven-day windows to stay well inside it. Measured on one account the cap binds a
response rather than a range: the legacy query stops 31 days back however wide the window,
while the paginated generic query returned every interval asked for.

## Statistics

Energy and cost are published as Home Assistant **external statistics**, not as recorder
history behind a sensor, because external statistics can be rewritten when a correction
arrives.

A pass computes every cumulative sum from every record it reads, and filters only at
publication. Projecting from the corrected interval onwards *with no starting total* was a
defect: the corrected hour looked like the first hour ever recorded and the cumulative sum
restarted from it.

**A pass reads from the current period boundary, not from the first month ever collected.**
Reading everything each time costs one pass over the whole ledger for every correction, which
grows without bound as history accumulates. Truncating is only sound on a period boundary,
because that is where the cost formula's cumulative kWh restarts — begin anywhere else and
every later hour is priced from a partial total. `billing_period.py` names those boundaries.
The projector remembers the total each series had reached at the two most recent ones and
resumes from it, so the sums are identical to a whole-ledger pass. It falls back to the whole
ledger when the boundary has no remembered total, which is the first pass after a restart and
also what makes an older correction correct. Two boundaries are what the refresh cadence
reaches; keeping one per month would grow without bound, and an older total stops being
trustworthy once a correction can rewrite the hours before it.

The totals are held in memory rather than read back from the recorder. The projector is the
only writer of these series, so its own last pass is the authority, and an empty cache costs
one whole-ledger pass rather than a wrong number.

Cost per hour is:

```
kWh × the price step this billing period's cumulative kWh has reached
  + kWh × (fuel-cost adjustment + renewable levy), for whichever is in force
  + the daily standing charge ÷ 24
```

An hour that crosses a step boundary is split across both prices. Export is never priced at a
consumption rate. A charge in a unit this formula cannot express makes the whole tariff
unusable rather than partly priced.

**Steps restart on the invoiced period, anchored on the reported meter-reading day.**
`billing_period.py` takes whichever evidence states the schedule most directly: two consecutive
scheduled reading dates that agree on a day one month apart, else the day billable supply
began, else the Asia/Tokyo calendar month. The anchor is clamped to the last day of a month too
short to hold it, so each period stays adjacent to the next.

The rule itself was measured on **one** account with **one** closed invoice.
`docs/API_CONTRACTS.md` records what each candidate field reported and which were rejected. The
diagnostics download reports the derived anchor, which evidence produced it, and whether the
provider's own `readingDateDayOfMonth` agrees — so an account this rule is wrong for can be
recognised from a bug report rather than guessed at.

**Nothing in the cost path assumes a plan shape.** A tariff whose charges vary by time of day,
mix two grid operators, or are measured in something other than consumed kWh is refused with a
recorded reason rather than approximated, and an hour is priced with the rate generation the
provider says was in force then. A single-price plan is priced from its one charge; a stepped
plan from its ladder. What the standing charge is measured in is reported rather than acted on,
because one account reported `YEN_AMPERE_DAY` and the set of possible values is unknown.

**The two per-kWh adjustments are archived, because the API forgets them.** `fuelCostAdjustment`
and `renewableEnergyLevy` arrive with the period they apply to, and the provider serves only the
one in force, so an hour from a replaced period can never be priced from the API again. Every
other input can be re-fetched. `tariff_history.py` keeps a private archive per supply point;
an hour outside every stored period is priced with the nearest stored value, the earliest for an
hour before the archive begins and the latest for one after it ends. Using the newest value for
every uncovered hour would price a two-year-old hour with this month's figure, which changes by
several yen per kWh and changes sign.

An archive that fails to load is put into **read-only quarantine**: the file is left exactly as
it is, pricing falls back to the live tariff, and a repair issue says so. The ledger recovers
from a corrupt partition by saving an empty one over it, which costs a re-fetch; doing that here
would cost the only copy.

**A price arriving provokes a statistics pass.** The tariff is read on a twelve-hour cadence
and a cost series is only ever written by a pass, which runs every thirty minutes; the two
clocks are independent, so after a restart a price could sit in hand for half an hour while
the Energy Dashboard showed energy and no money. The commercial coordinator's listener now
asks for a pass, marked from now — the readings have not moved.

**A change to any cost input republishes the whole cost series once.** `dirty_from` limits
publication to recent hours, so a corrected price, a moved period boundary, or a newly archived
adjustment would be computed for every hour and then discarded before it reached the recorder.
The projector fingerprints what a cost was last computed from and republishes when it differs.
The fingerprint is held in memory, so the first pass after a restart republishes — which is what
lets a correction to this formula reach rows an earlier version wrote. Energy rows are untouched,
because a price does not move them.

Every price comes from the customer's own agreement, so nothing is entered by hand. Against
one closed bill on one account the total came to 104% — a single measurement, taken before the
period alignment below, not a figure to expect. The README's known limitations say what still
makes a total differ.

## Coordination

| Work | Interval |
|---|---|
| The poll — readings | every 30 minutes, re-reading the last 72 hours |
| Discovery | every 24 hours |
| Contract and billing | every 12 hours |
| Full reconciliation | daily, over the current and previous month |
| Full history | on request, one seven-day window every three seconds |

Setup does not wait for history. It finishes from recent data and queues older windows for the
background worker. Checkpoints are persisted, so a restart resumes the queue instead of
rebuilding it. A failure affecting one direction leaves the other directions working.

The recorder is listed in `after_dependencies`, which orders setup but does not guarantee the
recorder is loaded. Statistics publication checks for it and logs one warning rather than
failing.

## Full history

Setup collects the current and previous month. Everything older is collected only when the user
presses the **Import full history** button on a supply point's device page.

**Progress is a cursor, not a plan.** One `DirectionBackfill` per direction records how far back
it has reached, and the window in flight is derived from it. Registering a window per step would
grow the checkpoint without bound and make every save quadratic, since each completion is matched
against its generation's windows on every write. Re-fetching a window costs nothing, because the
ledger is keyed. The walk is never named in `generations`, so a checkpoint written with this
feature still loads on a build without it.

**The walk stops at the reported supply start, or when it runs dry.** `supplyPeriods
.supplyStartAt` says where an account's readings begin, so reaching it ends the walk with no
wasted requests. Three consecutive empty windows — 21 days of silence — end it too, which covers
the two cases the supply start does not: an account that cannot read that field, and the gap
between two supply periods for a customer who moved out and back in. One empty window is not
evidence: a meter exchange or a provider gap each produce one. `BACKFILL_MAX_HISTORY` bounds
both, so a misreported start cannot send the walk to 1970.

**Pacing comes from the provider's own accounting.** A reading request costs a flat 17 points of
a 50,000-per-hour allowance, so one window every three seconds draws about a third of it; below a
20,000-point reserve the walk waits for the reset. Both reuse the retry controller's barrier,
which already composes with backoff and already lets ready work overtake a held scope.

**A walked window publishes nothing.** It flushes the ledger and moves the cursor. Projecting
statistics reads the whole ledger and rebuilding the snapshot re-reads every enabled supply
point, so doing either per window is what would make hundreds of windows unusable. Both happen
once, at whatever ends the walk — and once at the press, so the button is not silent for half
an hour. Neither reaches the provider: the snapshot is built from ledgers already written.

**A legacy answer stops the walk** with its cursor intact, because that path returns the most
recent 31 days however far back it is asked. `docs/adr/0009-user-triggered-history-backfill.md`
records the rest, including what collected history does *not* reach: the calendar sensors
aggregate only the current and previous month.

## Migrations

Three things are versioned and each is handled differently, because losing them costs
different amounts.

| What | Version | On an unrecognised version |
|---|---|---|
| The config entry | `ConfigFlow.VERSION` | `async_migrate_entry` in `__init__.py`. Nothing needs migrating yet; it exists because Home Assistant refuses to load an entry whose major version differs when no handler is defined, so its absence — not the schema change — would be what breaks entries at the next bump. An entry from a newer version is refused rather than read with older code |
| A ledger partition | `LEDGER_SCHEMA_VERSION` | migrated forward, record by record. A newer version is treated as corrupt and isolated, and a repair issue names the partition. The ledger is the only source of truth, so it is never discarded |
| A sync checkpoint | `CHECKPOINT_SCHEMA_VERSION` | discarded, and planning starts again from the current month |

**A checkpoint is discarded rather than migrated on purpose.** It records which windows were
already fetched, which is derived from the ledger, so throwing one away costs re-reading those
windows and loses no readings — the ledger is keyed, so a re-fetched interval replaces itself.
Failing instead is what the code used to do: `from_dict` raises on an unknown version, the poll
turns that into `UpdateFailed`, and the entry could never synchronise again, so raising the
checkpoint version would have broken every installation until the user deleted the entry and
lost the history with it. `diagnostics` counts the discards, so re-reading old windows has a
visible cause.

When a future checkpoint schema holds state worth carrying forward, migrate it inside
`SyncCheckpoint.from_dict` the way a ledger partition is migrated. The discard stays as the net
underneath that.

## Commercial data

Account status, agreements, and billing are three independent requests. Each records its own
availability — available, partial, forbidden, unsupported, or failed — so an account that may
not read agreement data still reports consumption. An authentication error is raised rather
than recorded as one of those states.

Financial entities are disabled by default.

## Diagnostics and repair issues

Diagnostics contain constants, counts, booleans, enumerated states, HMAC identities, and UTC
timestamps. A failure is reported by its exception class name, because API message text has no
bounded length or format. A test asserts that no account number, supply point number, address,
token, reading value, or amount appears.

Repair issues are informational. Each explains a condition the user cannot fix by
reconfiguring, and says whether anything needs doing. They cover a corrupt ledger partition,
readings that stopped arriving, an unavailable capability, a missing commercial permission,
a tariff whose shape the cost formula cannot express, an archive of past rate adjustments
that could not be read, and a supply point whose reading path cannot serve older readings. Reauthentication is not among them,
because Home Assistant owns that prompt.

The last of those exists because an absent cost statistic looks the same whether the plan
cannot be priced or the integration is broken. The `tariffs` section of the diagnostics
download carries the same distinction in more detail: each tariff's product type, step count,
number of rate generations, and what the provider says its standing charge is measured in.
None of that is a monetary amount.

## Rules that look like problems

Each of these is deliberate. Do not change one without a reason other than tidiness.

**`api/` has two kinds of parser, not duplicated helpers.** A strict parser raises
`OejpInvalidResponseError` for a field the integration depends on; a lenient one returns `None`
for optional data. Helpers with the same name differ between modules because the field's
intent differs. Merging them would change parser contracts silently.

**The poll and the background worker read separate failure tables.** They decide different
things: the poll records a per-attempt failure and may abandon the rest of the poll, while the
worker chooses between retrying with backoff, giving up, and reauthenticating. They share the
*classification* — a permanent worker failure is recorded with the class the poll's table
assigns — so the two can never disagree about what an exception means. Both tables are ordered
most-specific-first and both orderings are asserted by tests.

**`_async_publish_pending_statistics` is called with the lock held and without it.** The poll
calls it without the mutation lock, the background worker with it, and they can overlap. The
method removes a supply point's dirty marker only if it still matches the one it just
projected. Without that check, a correction arriving while a projection was running would be
dropped. Making the locking uniform would block the worker for the length of a projection, so
the behaviour is tested rather than changed.

**Extracting the coordinator's direction-status helpers would not shorten it.** Its two long
methods call those helpers rather than contain them, so moving them elsewhere leaves both
methods the same length.

## Invariants

Breaking any of these is a regression even when the tests pass.

1. API behaviour is not asserted without observing it. A behaviour read from documentation is
   unverified until a probe confirms it.
2. No raw identifier from the API reaches an entity ID, state, attribute, log, or the
   diagnostics download.
3. A correction replaces the earlier interval and rewrites every later total.
4. A period that is not fully covered reports `unknown`, never a partial sum.
5. Falling back to the legacy provider happens only for the four listed causes.
6. Setup completes without waiting for history.
7. Nothing under `api/` imports Home Assistant.
