# OEJP Home Assistant Integration — Master Technical Design v3

Status: normative implementation plan
Reviewed: 2026-08-03
Repository: `SteveShin323/ha-octopus-energy-japan`
Domain: `octopus_energy_japan`

## 1. Authority and objectives

This document is the normative project architecture and phase plan. Archived
designs are research history only. Durable decisions are recorded in `docs/adr/`.
More specific active specifications control within their scope:

- [`LEDGER_AND_AGGREGATION.md`](LEDGER_AND_AGGREGATION.md) controls ledger and
  deterministic aggregation semantics;
- [`RUNTIME_AND_ENTITIES.md`](RUNTIME_AND_ENTITIES.md) controls PR 7 runtime and
  entity behavior;
- [`ENERGY_STATISTICS.md`](ENERGY_STATISTICS.md) controls PR 8 recorder
  statistics projection;
- [`CONTRACT_AND_BILLING.md`](CONTRACT_AND_BILLING.md) controls PR 9 optional
  account, agreement, product, and billing behavior, and records the
  provider-cost verification gate; and
- [`PR7_DELIVERY_PLAN.md`](PR7_DELIVERY_PLAN.md) records the completed PR #14
  branch and child-PR procedure without changing runtime behavior.

The design is informed by the official OEJP GraphQL documentation and example,
plus code-level review of `strongbugman/ha-octopusenergy-oejp`,
`Shuangbing/oejp-hacs`, `mapplebox/oejp`, and `lvctr/hass-oejp`.

The project provides:

- a high-quality read-only Home Assistant integration for OEJP;
- HACS-first distribution with a future Home Assistant Core-compatible shape;
- multiple accounts, supply points, meters/registers, and import/export series;
- deterministic behavior after delayed, duplicated, corrected, omitted, or
  reordered readings and restarts;
- English normative contributor documentation and Japanese user documentation;
  and
- privacy-preserving diagnostics without external telemetry.

Release quality gates are:

- all required GitHub checks pass;
- line and branch coverage is at least 95%;
- authentication, ledger, statistics, and migration modules reach 100% coverage;
- no known P0 or P1 defects;
- supported config-entry lifecycle paths use the Home Assistant test harness; and
- release candidates pass real-account and clean HACS install/upgrade/removal
  matrices.

The project targets Home Assistant Gold requirements before beta and
Platinum-oriented async, typing, and efficiency practices for 1.0.

Current implementation status:

| Plan scope | Status |
|---|---|
| PR 1–7: foundation through runtime/entities | Implemented and validated |
| PR 8: Energy statistics | Implemented and validated |
| PR 9: contract, tariff, and billing | Implemented and validated, except provider cost and tariff rate projection |
| PR 10: diagnostics and Repairs | Planned |
| PR 11: release documentation and packaging | Planned |
| Provider-issued cost and tariff rate projection | Awaiting provider metadata or probe |
| Production OAuth metadata and real-account release matrix | Awaiting OEJP |

## 2. Authentication

### 2.1 Public behavior

The public integration never collects or retains an OEJP password.

```text
Add integration
  -> OEJP-hosted sign-in
  -> Authorization Code with PKCE S256
  -> access and refresh tokens
  -> GraphQL requests
  -> automatic refresh
  -> Home Assistant reauthentication after revoke or terminal refresh failure
```

Authorization Code with PKCE is primary. Device Authorization Grant is an
optional fallback if OEJP approves it. A client secret is never embedded or
distributed.

A shared public client ID may be committed only after OEJP confirms publication
and reuse across installations. If user-specific client IDs are required, use
Home Assistant Application Credentials. If a client secret is mandatory, do not
ship it and renegotiate public-client terms.

Tokens remain in the Home Assistant config entry and never appear in logs,
states, diagnostics, fixtures, issue templates, or telemetry. Authorization
scheme and scopes are implemented only after official confirmation or an
authorized redacted local probe.

### 2.2 Authentication abstraction

```python
class AuthSession(Protocol):
    async def async_get_authorization_header(self) -> str: ...
    async def async_refresh(self) -> None: ...
    async def async_revoke(self) -> None: ...
```

Implementations are `OejpPkceAuthSession`, optional
`OejpDeviceAuthSession`, deterministic `FakeAuthSession`, and a probe-only
`LegacyKrakenAuthSession`. Deprecated password operations never enter public
config flow or runtime.

### 2.3 OEJP application outcomes

| OEJP response | Project response |
|---|---|
| Shared public client and PKCE approved | Ship PKCE as default |
| PKCE and device grant approved | PKCE default, device fallback |
| Separate client IDs required | Register separate HA auth implementations |
| Device flow only | Support device flow as setup path |
| Client secret required | Do not ship; renegotiate |
| User-specific client ID required | Use Application Credentials |
| Broad customer scope only | Document and request least privilege |
| Application rejected | No functional public release; fixture development continues |

The status record captures client IDs per grant, redirect URIs, scopes,
permissions, token lifetimes, refresh rotation, authorization scheme,
generic/legacy access, billing access, and client-ID publication permission.

## 3. Config entries, identity, and privacy

One OAuth login identity owns one config entry and all resources visible to it.
The config-entry unique ID is:

```text
HMAC(local installation secret, issuer + OIDC subject)
```

Account and supply-point registry identities use installation-local HMACs over
provider identifiers. They are stable inside one installation and not
correlatable across installations.

Raw identifiers may exist only in private runtime/storage joins required to call
OEJP. Account numbers, supply-point identifiers, SPINs, meter/register IDs,
addresses, names, email, tokens, and raw readings/bills are excluded from public
states, attributes, names, logs, diagnostics, fixtures, and telemetry.

Active/unknown resources are enabled automatically. Historical resources are
discovered but disabled unless selected during reconfiguration. New points join
the existing entry. Closed or removed points retain registry, ledger, checkpoint,
and statistics continuity rather than being deleted.

The pre-alpha data format is not a compatibility contract. After first alpha,
every config or storage format change requires migrations and migration tests.

## 4. Architecture and type boundaries

```text
OAuth implementation / AuthSession
  -> gated authenticated GraphQL transport
  -> strict operations and parsers
  -> discovery and capability registry
  -> direction-scoped reading providers
  -> persistent interval ledger
  -> aggregation service
  -> PR 7 runtime synchronization and entity projection
  -> external statistics projector
  -> optional contract/tariff/billing coordinators
  -> diagnostics and repairs
```

Raw GraphQL dictionaries stop at parser boundaries. Coordinators and entities
consume typed domain models only.

`EnergyReading` contains account/supply-point/device/register join IDs,
direction, timezone-aware UTC interval, `Decimal` value, unit, granularity,
source/revision, quality, official provider cost when present, and observation
time.

Core types include `ReadingSeriesKey`, `CapabilitySnapshot`, `LedgerRecord`,
`CorrectionResult`, `AggregationSnapshot`, direction-scoped provider results,
runtime coverage/status snapshots, `StatisticsProjection`, and
`OejpRuntimeData`.

## 5. GraphQL operations, providers, and errors

### 5.1 Providers

`GenericReadingsProvider` handles current supply-point/device/register readings,
including import/export, units, granularity, pagination, and quality.
`LegacyHalfHourlyProvider` handles legacy half-hour and billing-period interval
operations, retaining version and official cost fields where available.

Provider selection is per supply point and direction. Generic is preferred.
Legacy fallback is allowed only for an allow-listed direction-specific disabled
field, unsupported configuration, permission gap, or schema compatibility
condition that legacy can represent.

Fallback is forbidden for authentication, rate limit, timeout, server,
malformed-response, and invalid-identifier failures. Mixed outcomes such as
generic import success and export failure preserve the successful direction.
Global schema capability is a probe hint only and never creates an entity.

### 5.2 Strict, optional, and typed failures

Strict operations include viewer identity, core discovery, and readings.
Optional operations include introspection details, balance, billing, tariff,
cost, and optional topology metadata. Partial data is accepted only through an
explicit optional execution path.

Failures are typed as authentication, authorization, rate/complexity/node limit,
validation/schema, not found, retriable HTTP/transport, non-retriable HTTP,
invalid response, or optional partial failure. Safe structured GraphQL
code/type/path and optional bounded `Retry-After` are preserved separately from
provider-rendered text.

One logical GraphQL operation per config entry is allowed in flight. The gate
covers authorization-header acquisition and the single refresh retry. Runtime
code never bypasses it.

## 6. Persistent interval ledger

The ledger persists authoritative interval records rather than only cumulative
values or a cursor.

```text
ReadingSeriesKey + UTC start_at + UTC end_at
```

Rules:

- identical content is a no-op;
- changed value/version/quality/official-cost is a correction;
- a successful provider snapshot is authoritative only for its exact direction,
  series, source family, and half-open window;
- conflicting duplicates in one response are parser errors;
- observation time and correction count are retained; and
- aggregates/statistics rebuild entirely from ledger data.

Storage uses versioned Home Assistant `Store` partitions by entry, HMAC point,
and month. Saves are atomic and debounced. Current/previous month stay resident;
older data loads lazily. Corruption is partition-isolated and later surfaced as
a repair without exposing payloads.

Property tests enforce order independence, idempotency, interval ordering,
correction behavior, and aggregation invariants.

## 7. Synchronization and scheduling

The fixed PR 7 runtime is non-blocking beyond recent data:

| Operation | Window/cadence and execution |
|---|---|
| Blocking first refresh | latest 72 hours only; no sleep or retry loop |
| Consumption poll | every 30 minutes, latest 72 hours |
| Initial month history | persistent background queue; previous/current JST month excluding final 72 hours |
| Daily reconciliation | persistent background queue; previous/current JST month |
| Query chunk | at most 7 days |
| Discovery | 24 hours |
| Background GraphQL concurrency | one logical operation per entry |
| Contract/tariff | 12 hours in later coordinator |
| Billing | 12 hours in later coordinator |
| Optional long backfill | background queue, up to 13 months |

One worker per entry executes direction-specific request scopes with coalesced
reason/generation obligations. Regular polls preempt the next background request.
Background checkpoints are private, HMAC-scoped, and persisted only after
affected ledger partitions are flushed. They retain completed windows,
daily barriers, and reconstructable historical coverage across restarts.

Rate limits honor valid `Retry-After`; other transient background failures use
bounded deterministic full-jitter exponential backoff. Setup and regular polls
do not sleep internally. Permanent failures do not spin. Authentication triggers
reauthentication.

Ledger timestamps are UTC. User periods and planning generations use
`Asia/Tokyo` boundaries. The complete executable contract is
[`RUNTIME_AND_ENTITIES.md`](RUNTIME_AND_ENTITIES.md) and ADR 0004.

## 8. Home Assistant devices and entities

```text
OEJP account device
  -> electricity supply-point device
     -> direction-specific entity series
```

Meters/registers remain internal series dimensions unless independently
manageable.

Enabled by default after successful direction discovery:

- latest reported interval energy;
- today, yesterday, this week, this month, and last month;
- latest reading timestamp;
- data delay;
- supply-point status; and
- data available.

Disabled by default in later work:

- latest interval average power;
- quality/correction count;
- rate-limit information;
- balance;
- bill/payment summary; and
- official cost estimate.

Names never claim live, real-time, or current power. Period totals are unknown
until authoritative query coverage spans the requested JST period. A successful
empty direction is queryable and can create entities; no individual zero
intervals are fabricated. Current direction failures affect only that direction.

Period sensors are convenience projections, not Energy Dashboard truth.
Device/state classes are finalized with recorder/statistics tests.

## 9. Energy Dashboard and statistics

External statistics are projected per supply point/direction for imported kWh,
exported kWh, and official JPY cost only when OEJP supplies it.

Projection produces hourly state and cumulative sum. On correction:

1. find earliest changed interval and affected hour;
2. load the preceding cumulative checkpoint;
3. recalculate hourly values through present;
4. regenerate every affected cumulative sum; and
5. update through supported Home Assistant recorder APIs.

Projection is idempotent across restart, duplicate fetch, and correction replay.
It consumes ledger and runtime coverage contracts without replacing them.

## 10. Account, contract, tariff, and billing

After consumption/statistics are stable, optional operations may add account
status/balance, agreement periods, product/tariff components, official
`costEstimate`, latest bill/due date, and summarized payment information.

Complete bills/transaction histories are not entity attributes. User-entered
simple `kWh × rate` cost is outside 1.0 because it cannot faithfully model
Japanese billing.

## 11. Diagnostics, repairs, and observability

Redacted diagnostics include versions, providers/capabilities, coordinator and
per-direction status, coverage, reading ranges/counts, corrections, ledger and
checkpoint health, projection status, safe rate-limit metadata, and redacted
GraphQL code/type/path.

They exclude tokens, email, raw IDs, addresses, names, raw readings, and raw
financial data. Repairs cover revoked OAuth, missing auth implementation/scope,
provider/schema changes, ledger/checkpoint migration or corruption, statistics
drift, and prolonged lack of readings. No external telemetry is sent.

## 12. Pull-request delivery sequence

1. **Design v3 and quality baseline** — architecture, ADRs, OAuth status,
   archived prior designs, strict typing, coverage gates, pinned Actions,
   security automation, and quality tracking.
2. **OAuth and AuthSession** — PKCE/Application Credentials, optional device
   abstraction, refresh/rotation/revoke/reauth, and password-flow removal.
3. **Safe real-account probe** — read-only allow list, PII substitution,
   synthetic fixtures, scanning, and provenance.
4. **Discovery and capabilities** — all accounts/resources, pagination,
   lifecycle, reconfiguration, and registry devices.
5. **Reading providers** — generic/legacy direction models, units, quality,
   revision/cost, strict fallback, and fixture contracts.
6. **Ledger and aggregation** — partitions, dedup/correction, migration/recovery,
   JST aggregation, and pure planning.
7. **Runtime and entities** — gated requests, bounded setup, persistent queue,
   retry/recovery, lifecycle, coverage, entities, and translations.
8. **Energy statistics** — import/export/cost projection, correction replay,
   recorder harness, and Energy Dashboard documentation.
9. **Contract, tariff, and billing** — only officially confirmed or sanitized
   probed operations, with partial-permission behavior.
10. **Diagnostics and recovery** — redaction, Repairs, drift/schema handling,
    and non-blocking API/OIDC monitoring.
11. **Documentation and release** — user documentation, templates, release
    process, HACS artifact, attestation, and clean lifecycle validation.

PR 7 was delivered through the sequential child-PR procedure in
[`PR7_DELIVERY_PLAN.md`](PR7_DELIVERY_PLAN.md). Its child PRs targeted
`codex/runtime-entities`, were squash-merged one at a time after complete CI,
and never targeted `main`. PR #14 is the sole integration PR to `main`; it was
marked ready only after every PR 7 requirement passed and is not auto-merged by
the delivery procedure. The maintainer subsequently squash-merged PR #14 into
`main` as commit `0349b79771b5b23a2ebbd63db00d4459c5661c4e` on 2026-08-03.

Each PR is complete only after its normative scope, tests, checks, and actionable
review findings are resolved.

## 13. Test matrix

Automated tests cover:

- HTTP 200 GraphQL errors/partial data and malformed payloads;
- exact HTTP/GraphQL classification, timeout, retry-after, rate limit, backoff,
  and offline authorization server;
- PKCE, refresh rotation, revoke, and reauthentication;
- request-gate concurrency across auth retry;
- multiple/duplicate accounts and resource addition/closure/reappearance;
- pagination, capability detection, and candidate-direction rules;
- only permitted per-direction generic-to-legacy fallback;
- import/export, units, nullable granularity, quality, and revision;
- reordering, duplication, correction, omission, and delayed arrival;
- ledger restart, atomic save, corruption, migration, and JST boundaries;
- background obligation/checkpoint durability and coverage reconstruction;
- setup, partial success, unload/reload, registries, reconfigure, and reauth;
- statistics correction/idempotency;
- diagnostics redaction and English/Japanese translations; and
- clean HACS packaging.

CI contains no real OEJP credential. Authorized validation uses the redacting
probe and only sanitized synthetic fixtures enter the repository.

## 14. Documentation policy

English is normative for repository design, API contracts, development, testing,
fixture redaction, contributing, security, privacy, and release. Japanese covers
installation, OAuth, entities, Energy Dashboard, delayed readings, privacy,
troubleshooting, diagnostics submission, and local-data removal.

Japanese user documentation and English/Japanese UI translations remain aligned
for public releases. Repository documentation is not maintained in Korean.

## 15. Release gates

- `0.1.x alpha`: OAuth, discovery, probe, and basic readings;
- `0.5.x beta`: ledger, runtime entities, Energy Dashboard, and diagnostics;
- `0.8.x beta`: import/export, agreements, official cost, and billing; and
- `1.0.0`: migrations, security, documentation, translations, and all quality
  targets.

No functional public release occurs before OEJP confirms client and permission
details.

A release candidate is tested with a real account for OAuth setup/refresh,
restart, multiple resources, generic/legacy directions, delayed/corrected data,
partial permissions, redaction, HACS lifecycle, and Energy Dashboard display.
