# OEJP Home Assistant Integration — Master Technical Design v2

Status: normative design baseline  
Reviewed: 2026-07-29  
Repository: `SteveShin323/ha-octopus-energy-japan`  
Target domain: `octopus_energy_japan`

## 1. Scope and authority

This document supersedes the conclusions in the earlier architecture notes where they conflict with this version. It incorporates the official OEJP GraphQL documentation, the official `octoenergy/oejp-api-example`, and four public Home Assistant-related repositories:

- `mapplebox/oejp`
- `Shuangbing/oejp-hacs`
- `lvctr/hass-oejp`
- `strongbugman/ha-octopusenergy-oejp`

The target is not a feature clone. The target is a production-quality, contributor-friendly integration that exceeds the strongest existing implementation in data correctness, authentication behavior, API adaptability, multi-account support, observability, testing, and long-term maintainability.

## 2. Revised executive conclusion

`strongbugman/ha-octopusenergy-oejp` is the strongest existing implementation reviewed. It changes the comparison baseline materially. It has the broadest account model, supports multiple accounts and supply points, models balances, transactions, bills and agreements, handles optional GraphQL fields, inserts external Home Assistant statistics, includes tests and release automation, and protects raw account and supply-point identifiers from normal entity states.

It is therefore the primary implementation reference. However, it still has architectural and correctness limitations that prevent adopting it wholesale:

- password authentication is repeated every 15-minute coordinator refresh;
- a large viewer snapshot plus two account-level reading queries are issued for every account each cycle;
- GraphQL errors are preserved but not classified into a stable domain exception taxonomy;
- the implementation depends on legacy `intervalReadings` and `halfHourlyReadings` shapes;
- interval readings do not include the reading `version` field, so corrections cannot be reconciled explicitly;
- external statistic cumulative sums are based on the last stored sum and the newly fetched window, which can make corrected historical hours difficult to propagate consistently;
- account identity still begins with email as the config-entry unique ID;
- a short, unsalted digest is privacy-improving but not a general-purpose secret-preserving identifier scheme;
- metadata, billing, readings and authentication share one fixed coordinator cadence.

The target project must combine:

1. `strongbugman`'s breadth, typed snapshot model, multi-account traversal, optional-field access status, privacy-conscious entity design, external statistics and project automation;
2. `Shuangbing`'s compact HA module separation, explicit delayed-data UX and bounded overlap concept;
3. `mapplebox`'s useful user-facing sensor inventory, JST aggregation and `Decimal` parsing;
4. `lvctr`'s blueprint-derived contributor tooling;
5. official OEJP schema, error metadata, rate limits and changelog as the source of truth.

## 3. Comparative assessment

| Project | Functional breadth | Multi-account | Data persistence | Energy Dashboard | API resilience | Tests/CI | Best use |
|---|---:|---:|---:|---:|---:|---:|---|
| `mapplebox/oejp` | Medium-high | No | Restored synthetic total only | Entity-based total | Low | Low | UX and sensor ideas |
| `Shuangbing/oejp-hacs` | Medium-low | No | Sync cursor only | No robust cumulative source | Medium-low | Limited | Small HA architecture and delay UX |
| `lvctr/hass-oejp` | None | N/A | N/A | N/A | N/A | High scaffold quality | Repository operations only |
| `strongbugman/ha-octopusenergy-oejp` | High | Yes | Recorder external statistics | Native external statistics | Medium-high | High | Primary implementation reference |
| Target project | High, staged | Yes | Version-aware interval ledger | Deterministic statistics | High | High | Long-term community implementation |

## 4. Detailed audit: `strongbugman/ha-octopusenergy-oejp`

### 4.1 Repository maturity

Observed strengths:

- standard HACS custom-component layout;
- manifest versioning, codeowner, documentation and issue tracker;
- embedded branding assets;
- `pytest` execution across Python 3.11, 3.12 and 3.13;
- compile checks, HACS validation and hassfest;
- automated ZIP packaging and release creation;
- standalone diagnostic tooling that reuses the API/model layer without importing Home Assistant.

This is significantly more mature than the other reviewed OEJP implementations.

Risks and decisions:

- The release workflow automatically tags every new manifest version pushed to `main`. The target should use an explicit release workflow or release PR so an accidental version edit cannot publish a release.
- CI actions should be pinned to immutable commit SHAs, not mutable major-version tags.
- Python support should follow the Home Assistant runtime version rather than testing unsupported combinations merely because the library imports successfully.
- Add Ruff, type checking, coverage thresholds, dependency review, CodeQL and migration tests.

### 4.2 API transport

Observed design:

- independent `httpx.AsyncClient` with persistent connection reuse;
- explicit timeout;
- user-agent containing integration version;
- HTTP, network and JSON failures wrapped in `GraphQLError`;
- token scrubbing in rendered errors;
- support for partial data via `execute_optional`;
- GraphQL documents separated from entity code.

Strengths to retain:

- persistent client lifecycle;
- explicit timeout and user-agent;
- optional-field requests returning both partial payload and error;
- transport layer independent of Home Assistant, enabling standalone tests and diagnostic tools;
- sanitization before rendering error text.

Limitations:

1. `response.raise_for_status()` is correct for HTTP failures, but GraphQL error classification ends at one generic `GraphQLError`.
2. `errors[].extensions.errorCode`, `errorType`, `errorDescription` and individual `path` values are not normalized into typed errors.
3. Retry policy, exponential backoff, rate-limit delay and request cancellation behavior are not explicit.
4. The `Authorization` header sends the raw token without a visible `JWT ` prefix. This may work with OEJP, but the accepted scheme must be confirmed and covered by an integration test against the official API behavior.
5. `httpx` is a valid choice, but adding an external runtime dependency is unnecessary if Home Assistant's shared `aiohttp` session can satisfy the same requirements. Shared HA session behavior should be preferred unless standalone reuse is judged more valuable.

Target decision:

Implement an HA-independent transport protocol but inject an HTTP adapter. The default HA adapter uses `async_get_clientsession`; standalone tools may use `httpx`. Both adapters must pass the same contract tests.

Required exception hierarchy:

```python
class OejpError(Exception): ...
class OejpTransportError(OejpError): ...
class OejpTimeoutError(OejpTransportError): ...
class OejpInvalidResponseError(OejpError): ...
class OejpGraphQLError(OejpError): ...
class OejpAuthenticationError(OejpGraphQLError): ...
class OejpAuthorizationError(OejpGraphQLError): ...
class OejpRateLimitError(OejpGraphQLError): ...
class OejpQueryValidationError(OejpGraphQLError): ...
class OejpNotFoundError(OejpGraphQLError): ...
class OejpSchemaChangedError(OejpGraphQLError): ...
```

Each exception retains sanitized structured errors and retryability metadata.

### 4.3 Authentication

Observed design:

- `obtainKrakenToken` requests access token, refresh token, refresh expiry and payload;
- `GraphQLToken` distinguishes access and refresh tokens;
- refresh token is deliberately retained for future support;
- coordinator calls `obtain_token(email, password)` on every 15-minute update.

Strengths:

- token semantics are documented correctly;
- refresh token is not accidentally used as the API access token;
- credentials are validated in config flow.

Critical limitation:

Password login is used as a polling operation. This repeats high-value credential handling, increases authentication traffic and ignores the refresh lifecycle exposed by the API.

Target design:

```text
Config flow login
  -> access token in memory
  -> refresh token in protected config-entry data if policy permits
  -> decode/record access expiry
  -> renew before expiry under asyncio.Lock
  -> retry one failed authenticated request
  -> password login only if refresh is unavailable or rejected
  -> ConfigEntryAuthFailed only after credential failure is confirmed
```

Until the OEJP refresh mutation is verified, cache the access token until expiry and perform password login only on expiry. Never log in every coordinator cycle.

### 4.4 GraphQL operation design

Observed operations:

1. a broad viewer snapshot fetching account identity, balance, transaction and bill samples, properties, addresses, supply points, meter serials, supply details and agreements;
2. `intervalReadings` for every account;
3. `halfHourlyReadings` for every account and a current period window.

Strengths:

- substantially richer model than the other projects;
- all accounts are traversed rather than selecting index zero;
- optional restricted fields are isolated so partial authorization does not break the entire integration;
- `costEstimate` is sourced from OEJP rather than a user-entered flat rate.

Limitations:

- the broad viewer query requests sensitive and slowly changing data every 15 minutes;
- transactions and bills are sampled even when no enabled entity needs the sample rows;
- request count scales approximately as `1 + 2 × account_count` every cycle;
- metadata, financial data and readings have different natural refresh cadences;
- legacy API response shapes remain embedded in the model;
- reading queries omit `version` and half-hour query omits `endAt`, reducing correction and interval validation capability;
- pagination is only used for small fixed samples rather than a reusable paging abstraction.

Target query groups:

| Query group | Default cadence | Purpose |
|---|---:|---|
| Authentication | token expiry driven | Obtain or refresh token |
| Discovery | 24 h and config reload | Accounts, properties, supply points, meters |
| Agreements/tariffs | 12–24 h | Active products and contract metadata |
| Consumption | 30 min | Recent reading overlap window |
| Reconciliation | daily | Wider correction/backfill window |
| Billing | 6–24 h | Balance, bills, transactions when enabled |
| Diagnostics | on demand | Rate limit and schema capability probes |

Queries must be purpose-specific and field-minimal.

### 4.5 Typed domain model

Observed strengths:

- broad dataclass model for account, property, supply point, meter, agreement, bill and transaction;
- explicit `AccessStatus` for authorized, unauthorized, disabled and error states;
- JST period aggregation;
- derived metrics include calculation notes and source metadata;
- account-to-supply-point iteration supports multi-account traversal.

Limitations:

- several numeric fields remain `Any` and are converted later;
- API DTOs and domain models are mixed in one large module;
- timestamps are often retained as strings instead of parsed timezone-aware datetimes;
- the model directly reflects the legacy GraphQL schema;
- reading direction, unit, version, quality and source generation are not represented uniformly;
- address, postcode, meter serial and raw identifiers are present in the in-memory snapshot even when entities do not need them.

Target separation:

```text
api/dto.py              Raw typed GraphQL DTOs
api/parsers.py          Schema validation and conversion
models/account.py       Stable account domain model
models/supply_point.py  Stable supply point domain model
models/reading.py       Stable EnergyReading model
models/billing.py       Stable billing model
services/aggregation.py Calendar and cumulative aggregates
```

Target reading model:

```python
@dataclass(frozen=True, slots=True)
class EnergyReading:
    supply_point_id: str
    direction: ReadingDirection
    start_at: datetime
    end_at: datetime
    value: Decimal
    unit: EnergyUnit
    version: str | None
    quality: ReadingQuality | None
    cost: Money | None
    source: ReadingSource
```

### 4.6 Multi-account and entity identity

Observed strength:

All accounts and all supply points in the returned snapshot are traversed. This is a major improvement over `accounts[0]` implementations.

Observed limitations:

- config flow unique ID is the lowercased email;
- one config entry owns every account returned by that login;
- changing the email while retaining the same OEJP accounts may create a new integration identity;
- account-specific reconfiguration or selective enablement is not part of config flow;
- short unsalted SHA-256 fingerprints are deterministic and hide raw identifiers from ordinary states, but they are not keyed pseudonyms.

Target design:

Use one credential entry that discovers accounts, with child devices per supply point. Persist a stable keyed local identifier mapping:

```text
HMAC-SHA256(local_random_secret, provider_identifier)
```

The local random secret is generated once and stored in integration storage. This prevents simple offline dictionary matching of predictable identifiers. Raw provider identifiers remain only in config-entry/private storage and redacted diagnostics.

Config flow must support:

- multiple accounts;
- account selection or all-account mode;
- supply-point inclusion/exclusion;
- reauthentication without changing entity unique IDs;
- migration from email-based unique IDs.

### 4.7 Aggregation and cumulative model

Observed design:

- current day, week and month are calculated from fetched half-hour readings using JST boundaries;
- cumulative consumption combines confirmed `intervalReadings` with later half-hour readings;
- interval cutoff attempts to prevent double counting;
- OEJP-provided `costEstimate` is used when available.

Strengths:

- much more defensible than a user-entered flat rate;
- source-aware cumulative composition;
- explicit cost completeness behavior;
- period calculations are self-healing within the fetched window.

Risks:

1. Reading `version` is not modeled, so same-interval corrections cannot be compared explicitly.
2. Interval and half-hour sources may overlap or use differing settlement boundaries.
3. String timestamps and missing `endAt` reduce validation strength.
4. A current week/month re-fetch does not guarantee older billing-period corrections are captured.
5. Combining interval and half-hour readings directly into one cumulative number assumes compatible semantics and units.
6. Cost estimates may be provisional and should not be labeled as final billed cost.

Target decision:

A persistent normalized interval ledger is mandatory. Aggregates are projections from the ledger, not stateful counters.

Ledger logical key:

```text
provider account
+ supply point
+ direction
+ UTC start
+ UTC end
+ unit
+ source series
```

Ledger record stores value, version, quality, cost estimate, fetch time and source. Upsert behavior replaces corrected values deterministically. Wider reconciliation periodically re-fetches historical windows.

### 4.8 External Home Assistant statistics

Observed strengths:

- hourly aggregation from half-hour readings;
- energy and cost statistics are separate;
- cost hour is omitted unless every contributing reading has a cost;
- stable statistic IDs use supply-point fingerprints;
- recorder statistics are more appropriate than a fragile restored sensor counter.

Critical risks:

- cumulative `sum` is seeded from the most recent stored sum before the new window;
- rows inside the fetched window may be overwritten, but later rows outside the window are not automatically recomputed when an old hour is corrected;
- if the fetch window overlaps an existing last statistic, base sum resets to zero, so continuity relies on recorder overwrite behavior and exact window composition;
- no explicit correction propagation algorithm updates all subsequent cumulative sums after a historical change;
- statistics APIs are internal Home Assistant APIs and require compatibility tests for every supported HA release;
- broad exception swallowing around prior-stat retrieval can silently reset behavior.

Target statistics strategy:

1. Store authoritative interval values in the integration ledger.
2. Detect changed hours after each upsert.
3. Rebuild hourly states for the changed range.
4. Recalculate cumulative sums from a stable checkpoint before the earliest changed hour.
5. Replace all affected subsequent statistics through the end of known data.
6. Keep official/provisional cost status in metadata or separate entities.
7. Test corrections, deletion, missing intervals, HA restart and recorder purge behavior.

### 4.9 Sensor semantics

Useful features to retain:

- account and property counts as diagnostics;
- account balance and status;
- active agreement/product information;
- supply-point status;
- today/week/month consumption and OEJP cost estimate;
- latest half-hour reading;
- latest reported interval average power, clearly labeled as historical average;
- access-status diagnostics;
- cumulative energy and cost statistics.

Required semantic changes:

- Never call delayed interval average power “live” or “current power.”
- Label `costEstimate` as OEJP estimate or provisional cost, not final bill.
- Disable high-cardinality financial samples and raw metadata by default.
- Do not place account number, SPIN, address, postcode or meter serial in state attributes.
- Use `EntityCategory.DIAGNOSTIC` for counts, access status and latest timestamp.
- Prefer one HA device per supply point and a service-level device for account-wide diagnostics.

## 5. Revised target architecture

```text
custom_components/octopus_energy_japan/
├── __init__.py
├── manifest.json
├── const.py
├── config_flow.py
├── options_flow.py
├── coordinator.py
├── sensor.py
├── diagnostics.py
├── services.yaml
├── strings.json
├── translations/
├── api/
│   ├── client.py
│   ├── transport.py
│   ├── auth.py
│   ├── errors.py
│   ├── operations/
│   │   ├── auth.graphql
│   │   ├── discovery.graphql
│   │   ├── legacy_readings.graphql
│   │   ├── supply_point_readings.graphql
│   │   ├── agreements.graphql
│   │   └── billing.graphql
│   ├── dto.py
│   └── parsers.py
├── models/
│   ├── account.py
│   ├── supply_point.py
│   ├── reading.py
│   ├── tariff.py
│   └── billing.py
├── providers/
│   ├── base.py
│   ├── legacy_half_hourly.py
│   └── supply_point_readings.py
├── storage/
│   ├── ledger.py
│   ├── migrations.py
│   └── identity.py
├── services/
│   ├── discovery.py
│   ├── aggregation.py
│   ├── reconciliation.py
│   ├── statistics.py
│   └── billing.py
└── entity.py
```

### Component responsibilities

- `api.transport`: HTTP only.
- `api.auth`: access/refresh token lifecycle and retry.
- `api.operations`: isolated GraphQL documents.
- `api.parsers`: validate response shape and convert DTOs.
- `providers`: adapt legacy and new reading APIs to `EnergyReading`.
- `storage.ledger`: persistent normalized readings and correction metadata.
- `services.discovery`: accounts, properties, supply points and capability detection.
- `services.aggregation`: pure deterministic functions.
- `services.statistics`: recorder projection and correction propagation.
- `coordinator`: schedules services and exposes an immutable snapshot.
- entity modules: presentation only; no GraphQL parsing or persistence.

## 6. Coordinator strategy

A single 15-minute “fetch everything” coordinator is rejected.

Recommended runtime:

```text
Reading coordinator       30 min
Metadata coordinator      12 h
Billing coordinator       6 h, only when enabled
Daily reconciliation      once per day
Manual full reconciliation service
```

The coordinator may share one client and token manager. It should coalesce concurrent refreshes and apply jitter to avoid synchronized API traffic after HA restarts.

## 7. Storage and migration

Use Home Assistant `Store` for the initial implementation with a compact schema and explicit version migration. If data volume becomes too large, migrate to SQLite through HA recorder-compatible mechanisms or a dedicated local database only after measuring real usage.

Storage must contain:

- schema version;
- keyed provider-ID mapping;
- selected accounts and supply points;
- normalized readings;
- source/version/quality metadata;
- reconciliation checkpoints;
- statistics projection checkpoint.

Retention policy:

- keep enough raw intervals to reconstruct dashboard statistics and recent corrections;
- support configurable retention only after safe defaults are proven;
- never delete data solely because a shorter polling window is selected.

## 8. Error, availability and reauthentication behavior

| Failure | HA behavior |
|---|---|
| Timeout/network failure | `UpdateFailed`, preserve last good state |
| API 5xx | bounded retry/backoff, then `UpdateFailed` |
| Rate limit | honor retry metadata, delayed refresh |
| Access token expiry | refresh and retry once |
| Invalid password/refresh token | `ConfigEntryAuthFailed` |
| Optional field unauthorized | retain core data, expose diagnostic access status |
| Schema mismatch | `OejpSchemaChangedError`, diagnostics with redacted path |
| Partial GraphQL response | parse valid data and record field-level error |
| Corrupt local store | quarantine, recover from API, create repair issue |

## 9. Test strategy

### Unit tests

- GraphQL error classification by `errorCode` and `errorType`;
- token expiry, refresh locking and single retry;
- timezone-aware parsing;
- account/supply-point discovery;
- legacy and new API adapters;
- duplicate and corrected readings;
- JST day/week/month boundaries;
- interval/half-hour overlap;
- provisional cost completeness;
- keyed identifier stability;
- diagnostics redaction;
- storage migrations.

### Contract fixture tests

Sanitized fixtures for:

- one account/one supply point;
- multiple accounts;
- moved or inactive property;
- multiple supply points;
- no access to interval readings;
- no access to half-hour readings;
- partial `data` plus `errors`;
- reading version correction;
- missing cost estimate;
- new `SupplyPointType.readings` shape.

### Home Assistant integration tests

- config flow and duplicate prevention;
- reauthentication;
- setup/unload/reload;
- device/entity registry stability;
- entity availability;
- recorder external statistics insertion;
- statistics correction propagation;
- diagnostics output;
- migration from early config entries.

## 10. Open-source operating model

Required repository files and automation:

- `CONTRIBUTING.md`
- `CODE_OF_CONDUCT.md`
- `SECURITY.md`
- `.github/CODEOWNERS`
- bug and feature issue forms
- pull request template
- architecture decision records under `docs/adr/`
- release checklist
- changelog
- Ruff and formatter
- type checker
- pytest with coverage threshold
- hassfest and HACS validation
- dependency review and CodeQL
- Dependabot or Renovate

Contribution boundaries must be explicit: new GraphQL fields enter through operations, DTO/parser tests and domain models before entities are added.

## 11. Phased implementation plan

### Phase 0 — Repository foundation

- adopt CI, issue forms, contributor documents and release policy;
- add pytest/ruff/type-check baseline;
- add architecture decision records;
- no public release yet.

Exit: clean CI and reproducible dev environment.

### Phase 1 — GraphQL transport and authentication

- structured errors;
- injected transport;
- access-token cache;
- expiry handling;
- refresh-token research and implementation;
- redaction tests.

Exit: no password login per poll and contract-tested retry behavior.

### Phase 2 — Discovery and identity

- typed accounts, properties and supply points;
- all-account traversal;
- selection options;
- keyed local identifiers;
- capability detection.

Exit: no production `[0]` selection and stable entities across reauthentication.

### Phase 3 — Legacy reading provider and ledger

- legacy `halfHourlyReadings` adapter;
- request `startAt`, `endAt`, `version`, `value`, `costEstimate` where supported;
- persistent ledger;
- overlap refresh;
- daily reconciliation.

Exit: duplicate/correction/restart tests pass.

### Phase 4 — Core Home Assistant entities

- latest interval energy;
- today/yesterday/week/month consumption;
- latest data timestamp and delay;
- access-status diagnostics;
- device grouping;
- disabled-by-default average-power entity.

Exit: entity semantics and diagnostics pass HA tests.

### Phase 5 — External statistics

- deterministic hourly projection;
- cumulative correction propagation;
- Energy Dashboard setup documentation;
- recorder compatibility tests.

Exit: historical correction test updates affected cumulative sums correctly.

### Phase 6 — Agreements, tariffs and billing

- active product/agreement entities;
- account balance and bill metadata;
- OEJP cost estimate entities;
- clearly separate estimate from issued bill;
- billing coordinator with slower cadence.

Exit: no financial field is represented with ambiguous semantics.

### Phase 7 — New supply-point reading API

- capability probe;
- `SupplyPointType.readings` provider;
- import/export direction;
- device/register/unit/quality mapping;
- fallback to legacy provider.

Exit: same domain and entity tests pass against both providers.

### Phase 8 — Public release

- security review;
- privacy review;
- Japanese, English and Korean translations;
- beta release;
- HACS submission readiness;
- schema/changelog release checklist.

## 12. Immediate implementation decisions

The next code work should be limited to foundation and Phase 1. Do not add broad sensors to the current skeleton yet.

Immediate tasks:

1. Replace the current placeholder client with structured transport and errors.
2. Implement `TokenManager` with access-token caching.
3. Add sanitized official-example fixtures.
4. Add multi-account discovery models.
5. Define ledger schema and migration tests before writing cumulative sensors.
6. Port the useful CI and project-operation patterns from `strongbugman` and the blueprint, with hardened release permissions.

## 13. Final design position

The strongest existing project proves that a rich OEJP integration is feasible. Our advantage should not be merely more sensors. It must be:

- fewer and better-scheduled API calls;
- no password login on every refresh;
- structured GraphQL error handling;
- explicit support for schema evolution;
- deterministic correction-aware energy statistics;
- stable multi-account identity;
- stronger privacy pseudonymization;
- clear semantics for provisional cost and delayed power;
- complete tests and contributor boundaries.

This is the standard against which implementation changes should be reviewed.