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

## Where the code lives

Grouped by responsibility. This is not a strict layering — see the dependency rule below.

| Group | Modules | Responsibility |
|---|---|---|
| Authentication | `oauth.py`, `oauth_metadata.py`, `password_auth.py`, `application_credentials.py`, `api/device_auth.py` | obtain and renew a bearer token |
| Transport | `api/client.py`, `api/auth.py`, `api/errors.py` | one GraphQL POST, error classification, retries |
| Operations | `api/discovery.py`, `api/readings.py`, `api/commercial.py`, `api/tariff.py`, `api/operations.py` | query documents and parsers, returning typed models |
| Ledger | `ledger.py`, `ledger_store.py` | store every interval, keyed so a correction replaces it |
| Aggregation | `aggregation.py` | Asia/Tokyo calendar totals over the ledger |
| Statistics | `statistics.py`, `statistics_runtime.py`, `tariff_cost.py` | external long-term statistics, energy and cost |
| Coordination | `coordinator.py`, `commercial_coordinator.py`, `sync.py`, `sync_runtime.py`, `sync_store.py`, `background_sync.py` | the poll, the background worker, checkpoints |
| Presentation | `sensor.py`, `binary_sensor.py`, `entity.py`, `runtime.py`, `diagnostics.py`, `issues.py` | entities, devices, diagnostics, repair issues |

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
cannot be corrected. Each interval is keyed by supply point, direction, and start time, so a
later version replaces the earlier one in place.

Storage is split into one partition per month. A partition that fails to load is skipped
rather than failing setup, and a repair issue reports which one.

Calendar totals use **Asia/Tokyo** day, week, and month boundaries. A period reports `unknown`
until every interval in it has arrived, so a half-synchronised day is never shown as a
complete day with a low number. These boundaries do not match a billing period, which ends at
a meter read a few hours after midnight.

One response is capped at 1488 intervals and silently drops the oldest beyond that, so
requests use seven-day windows to stay well inside it.

## Statistics

Energy and cost are published as Home Assistant **external statistics**, not as recorder
history behind a sensor, because external statistics can be rewritten when a correction
arrives.

The whole ledger is projected in one pass and filtered at publication. Projecting only from
the corrected interval onwards was a defect: the corrected hour looked like the first hour
ever recorded and the cumulative sum restarted from it.

Cost per hour is:

```
kWh × the price step this Tokyo month's cumulative kWh has reached
  + kWh × (fuel-cost adjustment + renewable levy), for whichever is in force
  + the daily standing charge ÷ 24
```

An hour that crosses a step boundary is split across both prices. Steps restart on the Tokyo
calendar month. Export is never priced at a consumption rate. A charge in a unit this formula
cannot express makes the whole tariff unusable rather than partly priced.

Every price comes from the customer's own agreement, so nothing is entered by hand. Against
one closed bill the total came to 104%; the README's known limitations say why.

## Coordination

| Work | Interval |
|---|---|
| The poll — readings | every 30 minutes, re-reading the last 72 hours |
| Discovery | every 24 hours |
| Contract and billing | every 12 hours |
| Full reconciliation | daily, over the current and previous month |

Setup does not wait for history. It finishes from recent data and queues older windows for the
background worker. Checkpoints are persisted, so a restart resumes the queue instead of
rebuilding it. A failure affecting one direction leaves the other directions working.

The recorder is listed in `after_dependencies`, which orders setup but does not guarantee the
recorder is loaded. Statistics publication checks for it and logs one warning rather than
failing.

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
readings that stopped arriving, an unavailable capability, and a missing commercial
permission. Reauthentication is not among them, because Home Assistant owns that prompt.

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
