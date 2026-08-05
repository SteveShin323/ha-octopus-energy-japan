# Architecture

How the integration is built, and the constraints that shaped it. The code and its
tests are the specification; this page exists so a contributor knows where to look and
which invariants must not be broken.

## Layers

Each layer depends only on the ones above it.

| Layer | Modules | Responsibility |
|---|---|---|
| Authentication | `oauth.py`, `oauth_metadata.py`, `device_auth.py`, `password_auth.py`, `application_credentials.py` | obtain and renew a bearer token for one of three sign-in methods |
| Transport | `api/client.py`, `api/auth.py`, `api/errors.py` | one GraphQL POST, structured error classification, retry policy |
| Operations | `api/discovery.py`, `api/readings.py`, `api/commercial.py`, `api/tariff.py`, `api/operations.py` | query documents and strict parsers, returning typed models |
| Ledger | `ledger.py`, `ledger_store.py` | persist every interval, keyed so a correction replaces rather than accumulates |
| Aggregation | `aggregation.py` | Asia/Tokyo calendar projections over the ledger |
| Statistics | `statistics.py`, `statistics_runtime.py`, `tariff_cost.py` | external long-term statistics, including cost |
| Coordination | `coordinator.py`, `commercial_coordinator.py`, `sync.py`, `sync_runtime.py`, `sync_store.py`, `background_sync.py` | refresh cadence, background backfill, checkpoints |
| Presentation | `sensor.py`, `binary_sensor.py`, `entity.py`, `runtime.py`, `diagnostics.py`, `issues.py` | entities, devices, diagnostics, repair issues |

`identity.py` sits beside all of them: every device, entity, and statistic is addressed
by an HMAC of an installation-local secret and the provider identifier, so no raw
identifier reaches Home Assistant's registries.

## Authentication

Three methods, selected in the config flow and recorded as `auth_method` on the entry.
`__init__.py` routes on it and refuses an unknown value rather than guessing.

- **Password** — `obtainKrakenToken` with email and password. The credential is stored
  because the refresh token lasts seven days and renewing it does not extend the expiry,
  so nothing else can sign in afterwards. A rejected renewal falls back to a full
  sign-in; a rejected sign-in is terminal and raises reauth.
- **OAuth authorization code** with PKCE S256, and **device authorization grant**. Both
  are public-client flows using Home Assistant's Application Credentials. Both need a
  published client ID.

One login owns one config entry. Switching method promotes the entry in place, keeping
its ledger and statistics and deleting any stored password. Removal purges the entry's
stored data, including the installation secret when it is the last entry.

## Reading providers

Two providers, with a strict fallback policy.

The **generic** provider calls `readings` at the most granular level discovered —
register, then device, then supply point — so the same energy is never counted at two
aggregation levels. Import and export are separate connections.

The **legacy** provider calls `halfHourlyReadings` by account and datetime range.

Fallback from generic to legacy is permitted only for an observed unsupported or
forbidden capability, an authorization error scoped to a reading child field, a
disabled-field error, or a null generic series after the supply point was found.
Authentication failures, rate limits, transport errors, malformed data, and unrecognised
validation errors stay visible. Widening that list is how a silent data-quality
regression gets shipped.

## Ledger and aggregation

**Raw intervals are persisted, not a running total.** The provider republishes intervals
with a new version when a billing period closes, and the values change. A running total
cannot be corrected; a keyed interval store can. Each interval is keyed by supply point,
direction, and start time, so a later version replaces the earlier one in place.

Storage is partitioned by month. A partition that fails to load is isolated rather than
failing setup, and a repair issue reports it.

Calendar projections use **Asia/Tokyo** day, week, and month boundaries. A period reports
`unknown` until it is fully covered, because a partly synchronised day is not a smaller
day. These boundaries deliberately do not match a billing period, which runs to a meter
read a few hours after midnight.

Request windows stay well inside the provider's per-response cap of 1488 intervals: the
integration asks for seven days at a time. Exceeding the cap truncates the oldest rows
silently, which would look like missing data rather than an error.

## Statistics

Energy and cost are published as **external statistics**, not recorder-backed sensor
history, because external statistics can be rewritten when a reading is corrected.

Projection is deterministic: the whole ledger is projected in one pass, then filtered at
publication. Projecting only from a correction boundary made a corrected hour look like
the first hour ever recorded, restarting the cumulative sum.

Cost is computed per hour as:

```
kWh × the price step the Tokyo month's cumulative kWh has reached
  + kWh × (fuel-cost adjustment + renewable levy), where each is in force
  + the daily standing charge ÷ 24
```

An hour crossing a step boundary is split across both prices. Steps restart on the Tokyo
calendar month. Export is never priced as consumption. A charge in a unit this formula
cannot express drops the tariff rather than pricing part of it.

Every input comes from the customer's own agreement, so the user enters no prices.
Measured against one real closed bill the result was 104% of the billed total; the two
causes are the billing boundary and the fuel-cost adjustment's missing history, both
recorded in the README's known limitations.

## Coordination

| Work | Cadence |
|---|---|
| Readings | every 30 minutes, re-reading the last 72 hours |
| Discovery | every 24 hours |
| Contract and billing | every 12 hours |
| Full reconciliation | daily, over the current and previous month |

Setup must not block on history. It completes from recent data and schedules older
windows as background work, with checkpoints persisted so a restart resumes rather than
restarts. A partial failure degrades one direction, never the whole entry.

The recorder is an `after_dependencies` entry, which orders setup but does not guarantee
the recorder exists. Statistics publication checks for it and warns once instead of
raising.

## Commercial data

Account status, agreements, and billing are three **independently optional** operations.
Each records its own availability — available, partial, forbidden, unsupported, or failed
— so an account that is not authorised for agreement data still reports consumption.
Authentication errors propagate instead of becoming an availability status.

Financial entities are disabled by default.

## Diagnostics and repair issues

Diagnostics contain only constants, counts, booleans, enumerated states, HMAC identities,
and UTC timestamps. Failures are reported by exception class name, because provider
message text is unbounded. A test asserts that no account number, supply point number,
address, token, reading value, or monetary amount appears.

Repair issues are **informational**: they explain a condition the user cannot fix by
reconfiguring, and each says whether action is needed. They are raised for corrupt ledger
partitions, silent readings, an unavailable capability, and a missing commercial
permission. Reauthentication is the one flow that is not a repair issue, because Home
Assistant owns that prompt.

## Deliberate shapes that look like problems

Each of these has been examined and left as it is. The reasoning is here so it is not
"fixed" into a defect.

**Two parsing disciplines in `api/`, not duplication.** `_optional_string`,
`_required_mapping` and friends appear in several modules with small differences. Those
differences are the point: a strict parser raises `OejpInvalidResponseError` for a field the
integration depends on, and a lenient one returns `None` for optional provider data.
`_optional_datetime`, `_optional_decimal`, `_optional_scalar_string`, `_required_datetime`
and `_required_identifier` each differ deliberately between modules. Extracting them into a
shared helper would silently change parser contracts. Only three are genuinely identical,
totalling about twelve lines, which is not worth a new import edge.

**`_async_publish_pending_statistics` runs under two lock disciplines.** The poll calls it
without the mutation lock; the background worker calls it with the lock held. They can
overlap, because the worker re-checks `_poll_pending` only before its network request. The
method pops a dirty marker only when it still equals the one just projected, which is what
keeps a change arriving mid-projection from being discarded. Making the discipline uniform
would mean holding the lock across a projection and blocking the worker for its duration —
a latency change that would need measuring first. The invariant is documented at the method
and pinned by tests instead.

**`OejpDataUpdateCoordinator` is large, and splitting out its status bookkeeping would not
help.** Extracting the eight direction-status methods into their own collaborator was
designed and rejected: they are called from about forty sites, and removing them leaves both
long methods exactly as long, because those methods *call* the helpers rather than contain
them. The length that mattered was a ninety-line exception ladder inside
`_async_update_data`, which is now a table. `_async_background_worker` remains long for a
different reason — the poll-yield handshake and retry bookkeeping — and is a separate
question.

## Invariants

Breaking any of these is a regression even when the tests pass:

1. Provider behaviour is never asserted without an observation. A shape taken from
   documentation alone is unverified until a probe confirms it.
2. A raw provider identifier never reaches an entity ID, state, attribute, log, or the
   diagnostics download.
3. A corrected interval replaces its earlier version and rewrites every later total.
4. A period that is not fully covered reports `unknown`, never a partial sum.
5. Fallback from the generic to the legacy provider happens only for the listed causes.
6. Setup completes without waiting for history.
