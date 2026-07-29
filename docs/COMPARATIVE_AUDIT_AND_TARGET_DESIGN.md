# OEJP Home Assistant Integration

## Comparative implementation audit and target design

Status: normative design document  
Repository: `SteveShin323/ha-octopus-energy-japan`  
Target domain: `octopus_energy_japan`  
Reviewed: 2026-07-29

## 1. Purpose

This document records a code-level review of the following projects and converts the findings into a concrete target architecture for this repository:

- Official OEJP GraphQL documentation and changelog
- Official `octoenergy/oejp-api-example`
- `mapplebox/oejp`
- `Shuangbing/oejp-hacs`
- `lvctr/hass-oejp`

The goal is not to aggregate every feature from the existing projects. The goal is to produce a public Home Assistant integration that is more correct, more resilient to delayed and corrected meter data, more compatible with multiple accounts and supply points, easier to test, and easier for outside contributors to extend without coupling new features to the transport layer.

This audit distinguishes three categories:

- **Observed**: directly present in the reviewed repository.
- **Risk**: a failure mode implied by the implementation.
- **Decision**: the behavior this project will adopt.

## 2. Executive assessment

| Project | Functional implementation | HA architecture | Data correctness | Extensibility | Project operations | Overall role |
|---|---:|---:|---:|---:|---:|---|
| `mapplebox/oejp` | High for a small project | Low–medium | Low–medium | Low | Low | Feature prototype |
| `Shuangbing/oejp-hacs` | Medium | Medium | Medium-low | Medium-low | Low–medium | Better HA MVP |
| `lvctr/hass-oejp` | None | Template-quality | N/A | Template-quality | High | Scaffolding reference only |

No reviewed repository is an appropriate codebase to fork wholesale.

The target implementation should borrow:

- user-visible feature discovery from `mapplebox/oejp`;
- async client/coordinator/entity separation and overlap-window polling from `Shuangbing/oejp-hacs`;
- CI, issue templates, devcontainer and contributor workflow from the blueprint retained in `lvctr/hass-oejp`;
- API behavior, schema names, error metadata and evolution rules from official OEJP documentation.

It must replace the following shared assumptions:

1. selecting `accounts[0]`, `properties[0]`, and `electricitySupplyPoints[0]`;
2. treating the legacy `halfHourlyReadings` response shape as the permanent domain model;
3. using email as the sole integration identity;
4. deriving an Energy Dashboard total by simply adding unseen timestamps;
5. classifying authentication failures by matching words in error strings;
6. treating delayed interval-average power as current power;
7. treating a flat user-entered unit rate as an official bill.

## 3. Official OEJP API implications

### 3.1 Transport and GraphQL errors

The endpoint is:

```text
https://api.oejp-kraken.energy/v1/graphql/
```

GraphQL operation errors may be returned with HTTP 200. The client must therefore distinguish:

1. DNS, connection and timeout failures;
2. HTTP failures;
3. invalid or unexpected content type;
4. JSON decoding failure;
5. GraphQL `errors`;
6. partial `data` plus `errors`;
7. schema parsing failure.

The official error structure exposes information such as `extensions.errorType`, `extensions.errorCode`, `extensions.errorDescription`, and `path`. Those fields, not rendered message text, must drive exception classification.

Target exception taxonomy:

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
```

Each exception should retain sanitized structured errors for diagnostics while redacting tokens, email, account numbers, supply-point identifiers, addresses, names, and payment information.

### 3.2 Authentication

The official sample uses `obtainKrakenToken` with email and password and receives an access token plus refresh-related fields.

Required token behavior:

- validate credentials during config flow;
- keep the access token in memory;
- serialize token refresh with an `asyncio.Lock`;
- renew shortly before expiry;
- retry one failed authenticated operation after renewal;
- use a confirmed refresh-token mutation when verified against the current OEJP schema;
- fall back to password authentication only when necessary;
- start Home Assistant reauthentication only after refresh and password authentication are genuinely invalid.

The implementation must not log in on every polling cycle.

### 3.3 Reading API generations

The reviewed projects use the legacy path:

```text
viewer.accounts
account.properties.electricitySupplyPoints.halfHourlyReadings
```

The official changelog also documents a newer supply-point reading model introduced and changed over time:

```text
Query.supplyPoint
SupplyPointType.readings
SupplyPointType.device
Device.readings
DeviceRegister.readings
units
reading qualities
```

The Home Assistant layer must not depend on either GraphQL response shape. Both must map into a stable internal reading model.

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
    source: ReadingSource
```

### 3.4 Rate limits and schema evolution

The official documentation describes complexity, node, and request limits. The design must use small purpose-specific operations and separate refresh cadences:

- readings: 30–60 minutes;
- account and supply-point discovery: 12–24 hours;
- agreement and tariff metadata: 12–24 hours;
- billing data: 6–24 hours;
- historical reconciliation: daily and on demand.

GraphQL documents must live outside entity modules and have fixture-based parser tests. A changelog review should be part of every release process.

## 4. Audit: `mapplebox/oejp`

### 4.1 Repository and packaging

**Observed**

The integration files are stored at repository root rather than under `custom_components/oejp/`. The manifest declares:

- domain `oejp`;
- version `0.1.0`;
- documentation and issue tracker pointing to the official sample repository rather than the implementation repository;
- empty codeowners;
- cloud polling and service integration type.

**Risk**

Root-level integration files are not a standard HACS integration layout. Misdirected documentation and issue links make support and contribution difficult. Empty codeowners and no visible CI or tests weaken maintainability.

**Decision**

Use the standard `custom_components/octopus_energy_japan/` layout, point documentation and issue tracker to this project, declare codeowners, and validate every pull request with HACS and hassfest.

### 4.2 API client

**Observed**

The API client correctly uses Home Assistant's shared `aiohttp` session, parses datetimes with offsets, parses meter values using `Decimal`, and treats `version` as a string. It decodes JWT expiry and reauthenticates near expiry.

It queries:

```graphql
viewer { accounts { number } }
```

and then:

```graphql
account(accountNumber: $accountNumber) {
  properties {
    electricitySupplyPoints {
      halfHourlyReadings(...) {
        startAt
        endAt
        version
        value
      }
    }
  }
}
```

It selects the first account, first property, and first electricity supply point.

GraphQL errors are classified by searching the rendered error text for `Unauthorized` or `UNAUTHENTICATED`.

**Strengths to retain**

- shared `aiohttp` session;
- timezone-aware parsing;
- `Decimal` at the API boundary;
- JWT-expiry awareness;
- treating `version` as opaque rather than forcing it to an integer;
- separate recent and longer historical queries.

**Risks**

- first-object selection silently produces the wrong result for multi-account customers, moved customers, closed properties, multiple meters, and import/export combinations;
- string matching cannot reliably distinguish invalid credentials, expired token, authorization failure, schema validation, and rate limiting;
- the client owns aggregation logic, mixing transport and domain services;
- no timeout policy is visible around the request;
- raw GraphQL errors are logged and may contain sensitive context;
- refresh-token fields are requested but not used;
- the legacy response shape is embedded directly in the client.

**Decision**

Split transport, authentication, discovery, reading provider, and aggregation into separate modules. Every list returned by discovery must be modeled and selected explicitly rather than indexed at zero.

### 4.3 Polling and aggregation

**Observed**

Each coordinator refresh performs:

- a recent 12-hour reading query;
- a query from the beginning of the previous month through now;
- local JST aggregation for today, yesterday, month-to-date, and previous month.

The coordinator polls every 900 seconds and converts authentication failures into generic `UpdateFailed` rather than `ConfigEntryAuthFailed`.

**Strengths to retain**

- JST calendar boundaries;
- separate recent and historical windows;
- coordinator-based polling.

**Risks**

- repeatedly loading up to two months every 15 minutes is wasteful and increases rate-limit exposure;
- all aggregates disappear if the current API response is incomplete;
- late corrections and missing intervals are not reconciled against persistent history;
- authentication failures do not trigger the HA reauthentication flow;
- a fixed 15-minute interval is unnecessarily aggressive for delayed 30-minute data.

**Decision**

Persist a normalized interval ledger, poll a bounded overlap window, perform daily wider reconciliation, and compute aggregates from the ledger. Use `ConfigEntryAuthFailed` only for actual credential failure.

### 4.4 Sensor semantics

**Observed**

The project exposes latest half-hour energy, derived power, calendar totals, flat-rate cost estimates, and a restored cumulative energy sensor.

Power is calculated as:

```text
interval kWh × 2000 = average watts over 30 minutes
```

Cost is calculated as:

```text
usage kWh × user-entered JPY/kWh
```

The cumulative sensor restores its previous total and adds readings whose `end_jst` string is later than the restored timestamp.

**Strengths to retain**

- useful sensor inventory;
- awareness of Home Assistant device/state classes;
- options flow for user configuration;
- attempt to support Energy Dashboard.

**Risks**

- the entity named `OEJP Power` is not current power; it is delayed interval-average power;
- lexical timestamp comparison is fragile if offset formatting changes;
- cumulative state ignores corrected values for previously seen intervals;
- missed intervals older than the recent window are never added;
- a restored total cannot be reproduced or audited;
- the flat-rate cost omits base charges, tiered rates, fuel adjustment, renewable levy, discounts, tax, and rounding;
- account number is added to normal entity attributes, exposing a stable customer identifier to every state export.

**Decision**

- Name the derived entity `Average power for latest reported interval` and disable it by default.
- Treat flat-rate cost as an explicitly labeled simple estimate and disable it by default.
- Compute Energy Dashboard totals deterministically from a persisted ledger.
- Keep sensitive identifiers out of state attributes; expose sanitized metadata only through diagnostics.

## 5. Audit: `Shuangbing/oejp-hacs`

### 5.1 Repository structure

**Observed**

This project follows a recognizable HA custom integration layout with `api.py`, `models.py`, `coordinator.py`, `sensor.py`, config flow, translations, manifest, and HACS metadata.

**Strengths to retain**

- clear minimum module separation;
- use of `SensorEntityDescription`;
- use of runtime data containing client and coordinator;
- standard setup/unload/update-listener lifecycle;
- configurable scan interval and synchronization days;
- diagnostic timestamp entity;
- explicit source reading timestamps.

### 5.2 API client

**Observed**

The async client is small and readable. It checks the GraphQL `errors` array and sorts parsed readings by `end_at`.

It still selects the first account, property, and supply point. Authentication errors are inferred from words such as `invalid` and `credential` in the string representation of all GraphQL errors.

**Risks**

- same multi-account and multi-supply-point failure as the first project;
- fragile error classification;
- no typed representation for accounts, properties, or supply points;
- no support for partial GraphQL responses;
- no explicit timeout/retry/backoff policy;
- legacy reading path only.

**Decision**

Retain the compact async client style, but move GraphQL operations and parsing into typed provider modules with structured errors.

### 5.3 Token handling

**Observed**

A later commit changed token handling to obtain a new JWT using email and password on every coordinator refresh because the token expires quickly.

**Risk**

This avoids stale-token bugs but increases authentication traffic, repeatedly handles the password, ignores refresh-token capabilities, and may encounter login-specific rate limiting or account defense mechanisms.

**Decision**

Use a dedicated token manager with expiry, lock, one-time retry, and verified refresh behavior. Password authentication is not a polling operation.

### 5.4 Storage and synchronization

**Observed**

The coordinator uses Home Assistant `Store`, but it stores only:

```json
{"last_synced_end_at": "..."}
```

It does **not** persist the reading list. Each refresh fetches from either the configured initial lookback or `last_synced_end_at - OVERLAP_HOURS`. The returned readings exist only in the current coordinator snapshot.

This is an important distinction: it is a persistent synchronization cursor, not a persistent delayed-reading cache or ledger.

**Strengths to retain**

- overlap-window idea;
- bounded configurable initial lookback;
- saving a sync watermark;
- explicit calculation of data delay.

**Risks**

- after restart, the cursor may cause the integration to fetch only the overlap window while no earlier readings are present in memory;
- today's total may be incomplete when the overlap does not reach local midnight;
- a six-hour overlap cannot detect corrections older than six hours;
- only the latest end timestamp is stored, so revisions cannot be compared;
- `version` is surfaced but not used for reconciliation;
- reading deletion or replacement cannot be represented;
- no ledger means no deterministic monthly or cumulative totals.

**Decision**

Persist readings, not just a cursor. The store must merge records by a logical interval key, retain version/quality metadata, and support pruning and schema migration.

### 5.5 Sensors and state model

**Observed**

The project exposes latest half-hour usage, today's total, and latest-data timestamp. The latest usage entity is energy with `MEASUREMENT`; today's total is energy with `TOTAL`. Up to 48 recent readings are embedded in a state attribute.

**Strengths to retain**

- small default entity set;
- timestamp diagnostic entity;
- source timestamps and data delay;
- use of device information and translated entity names.

**Risks**

- embedding 48 reading objects in state attributes creates recorder churn and unnecessarily large states;
- account number is exposed as device serial number and state attribute;
- device identifier is based on config-entry ID rather than a stable account/supply-point identifier;
- today's total is calculated only from the current fetch window and can be incomplete;
- no explicit availability logic for stale data versus API failure.

**Decision**

Do not place historical arrays in entity attributes. Expose only compact metadata: interval start/end, delay, quality summary, and last successful update. Detailed sanitized history belongs in diagnostics or a service response.

## 6. Audit: `lvctr/hass-oejp`

### 6.1 Actual status

**Observed**

The repository is essentially the `ludeeus/integration_blueprint` template. Its README states that it is a blueprint rather than an end-user integration. Its manifest still uses `integration_blueprint`, `Integration blueprint`, blueprint documentation, and blueprint codeowner values.

It does not implement OEJP authentication, GraphQL operations, account discovery, reading parsing, OEJP entities, or OEJP config flow.

**Conclusion**

It must not be counted as an existing OEJP implementation.

### 6.2 Useful project scaffolding

The repository still provides useful operational patterns:

- devcontainer;
- Ruff lint and format checks;
- hassfest and HACS validation;
- Dependabot;
- issue forms;
- contributing guide;
- development scripts;
- release-oriented repository structure.

**Decision**

Borrow the project hygiene, not runtime code. Remove stale blueprint names, links, copyright assumptions, and irrelevant dependencies.

## 7. Cross-project failure matrix

| Concern | mapplebox | Shuangbing | lvctr blueprint | Target |
|---|---|---|---|---|
| Async HA session | Yes | Yes | Template | Yes |
| Structured GraphQL errors | No | No | N/A | Yes |
| Token expiry awareness | Yes | Re-login every poll | N/A | Token manager |
| Refresh token | Requested, unused | Not used | N/A | Verify and use |
| Multiple accounts | No | No | N/A | Yes |
| Multiple properties | No | No | N/A | Yes |
| Multiple supply points | No | No | N/A | Yes |
| Import/export distinction | No | No | N/A | Domain model |
| Legacy readings | Yes | Yes | No | Adapter |
| New readings API | No | No | No | Adapter |
| Persistent readings | No | No; cursor only | No | Yes |
| Correction/version merge | No | No | No | Yes |
| Deterministic Energy total | No | No | No | Yes |
| Correct stale-data model | Partial | Better | No | Yes |
| Tests | Not demonstrated | Not demonstrated | Template guidance | Required |
| CI | Minimal/none | Minimal/none | Strong template | Strong |
| Contributor governance | Minimal | Minimal | Template | Project-specific |

## 8. Target architecture

### 8.1 Layer boundaries

```text
Home Assistant config flow / options / reauth
                │
                ▼
Integration runtime and coordinators
                │
        ┌───────┴────────┐
        ▼                ▼
Domain services      Diagnostics
        │
        ▼
Repository / interval ledger
        │
        ▼
OEJP API facade
  ├── token manager
  ├── account discovery
  ├── legacy reading provider
  ├── supply-point reading provider
  ├── agreement provider
  └── billing provider
        │
        ▼
GraphQL transport
```

Entities consume immutable domain snapshots. They never parse GraphQL dictionaries, authenticate, perform storage writes, or decide reconciliation policy.

### 8.2 Proposed package layout

```text
custom_components/octopus_energy_japan/
├── __init__.py
├── manifest.json
├── const.py
├── config_flow.py
├── coordinator.py
├── entity.py
├── sensor.py
├── binary_sensor.py
├── diagnostics.py
├── services.yaml
├── strings.json
├── translations/
│   ├── en.json
│   ├── ja.json
│   └── ko.json
├── api/
│   ├── __init__.py
│   ├── client.py
│   ├── auth.py
│   ├── errors.py
│   ├── models.py
│   ├── operations/
│   │   ├── auth.py
│   │   ├── accounts.py
│   │   ├── legacy_readings.py
│   │   ├── readings.py
│   │   ├── agreements.py
│   │   └── billing.py
│   └── providers/
│       ├── legacy.py
│       └── supply_point.py
├── domain/
│   ├── models.py
│   ├── discovery.py
│   ├── aggregation.py
│   ├── reconciliation.py
│   └── costing.py
└── storage/
    ├── ledger.py
    ├── migrations.py
    └── serialization.py

tests/
├── fixtures/
│   ├── graphql/
│   └── stores/
├── test_api_client.py
├── test_auth.py
├── test_discovery.py
├── test_reconciliation.py
├── test_aggregation.py
├── test_config_flow.py
├── test_coordinator.py
├── test_sensor.py
└── test_diagnostics.py
```

### 8.3 Discovery model

The config flow must perform explicit discovery:

```text
credentials
→ viewer accounts
→ active/eligible account selection
→ properties and supply points
→ import/export and meter/register discovery
→ create account-scoped config entry
→ create one HA device per supply point
```

Recommended identity rules:

- config entry unique ID: stable selected OEJP account identifier;
- device identifier: stable supply-point identifier within the account;
- entity unique ID: account + supply point + direction + entity key;
- never use email as the sole unique ID;
- never expose raw account or supply-point identifiers in entity state.

Multiple accounts should be configurable independently or represented as multiple discovered account entries. One account entry may own multiple supply-point devices.

### 8.4 Interval ledger

Logical key:

```text
account_id
+ supply_point_id
+ direction
+ start_at UTC
+ end_at UTC
+ unit
```

Merge policy:

1. Normalize timezone-aware timestamps to UTC.
2. Validate `end_at > start_at`.
3. Validate supported units and non-invalid numeric values.
4. Insert unseen intervals.
5. For an existing interval, replace data when version or quality indicates a newer authoritative record.
6. If version ordering is not defined, use deterministic source precedence and fetched-at metadata while retaining conflict diagnostics.
7. Record correction count and last reconciliation time.
8. Keep enough history for Energy Dashboard and requested aggregates; prune only under an explicit retention policy.

Store format must be versioned and migrated. Writes should be atomic through Home Assistant `Store` and serialized to avoid concurrent coordinator writes.

### 8.5 Polling and reconciliation

Normal update:

```text
last known interval end - overlap window
→ now
→ merge into ledger
→ recompute affected aggregates
```

Daily reconciliation:

```text
start of previous local day or a configurable wider window
→ now
→ merge corrections and late arrivals
```

Initial backfill:

- default 35 days, bounded;
- fetched in chunks rather than one oversized query;
- progress should not block config flow indefinitely;
- first entities may be created after a minimum recent window, followed by background coordinator-driven backfill only when HA supports it safely within the current execution; no promise of out-of-band work.

A manual service may request a bounded reconciliation range.

### 8.6 Aggregation semantics

All aggregation is done from ledger records using `Asia/Tokyo` calendar boundaries.

Default entities per import supply point:

- latest reported interval energy;
- today energy;
- yesterday energy;
- month-to-date energy;
- previous-month energy;
- cumulative imported energy suitable for Energy Dashboard;
- latest reading timestamp;
- data delay;
- data freshness/availability binary sensor.

Disabled-by-default entities:

- latest reported interval average power;
- simple flat-rate cost estimate;
- API rate-limit diagnostics;
- correction count;
- reading-quality diagnostics.

Cumulative energy must be deterministic. A practical initial definition is the sum of all retained authoritative interval values from a fixed integration epoch. The epoch and retention policy must be documented. The value must not be incremented solely from the latest response.

### 8.7 Cost model

Three concepts must never be conflated:

1. **Official cost or bill** returned by OEJP.
2. **Tariff-derived estimate** using all available tariff components.
3. **Simple flat-rate estimate** using user-entered JPY/kWh.

Each must use a distinct entity key and name. Flat-rate estimation is optional and disabled by default. Monetary calculations retain `Decimal` until final presentation.

### 8.8 Availability and stale data

The API is delayed by nature. Therefore:

- no new interval is not automatically an integration failure;
- API request failure and stale source data are separate states;
- sensor availability should follow coordinator request health;
- a diagnostic freshness sensor reports source delay;
- binary freshness thresholds should be configurable only within safe bounds;
- attributes should indicate the source interval end and last successful API update, not large historical arrays.

## 9. Home Assistant implementation requirements

### 9.1 Config flow

Required steps:

- credentials;
- connection/authentication validation;
- account selection when multiple exist;
- supply-point discovery summary;
- duplicate prevention based on stable account identity;
- reauthentication flow;
- migration support;
- distinct errors for invalid auth, cannot connect, rate limited, no account, and unsupported account shape.

Advanced options such as API URL should not appear in the ordinary user form unless there is a real supported use case. Developer-only endpoint override can be hidden behind an advanced option.

### 9.2 Coordinator design

Use separate cadences or internal freshness timestamps for:

- consumption;
- metadata;
- tariff/agreement;
- billing.

Do not perform all queries on every update. Use coordinator exceptions correctly:

- `ConfigEntryAuthFailed` for invalid credentials or irrecoverable refresh failure;
- `UpdateFailed` for transient transport, rate limit, or parsing failures;
- setup-not-ready semantics for temporary initial connectivity failure.

### 9.3 Entity descriptions

Use typed descriptions rather than per-entity subclasses where possible. Entities should include:

- translation keys;
- stable unique IDs;
- correct device and state classes;
- suggested display precision;
- entity categories for diagnostics;
- disabled-by-default flags for inferred or specialist values.

Avoid account numbers, email, address, and full historical readings in state attributes.

### 9.4 Diagnostics

Diagnostics should include sanitized:

- integration version;
- selected API provider generation;
- number of accounts/properties/supply points discovered;
- reading count and retained range;
- latest interval timestamp;
- data delay;
- last update result;
- correction/conflict counts;
- GraphQL error codes without raw sensitive payloads;
- storage schema version.

Redact:

- email and password;
- access and refresh tokens;
- account and ledger numbers;
- supply-point and meter identifiers;
- names and addresses;
- billing/payment details.

## 10. Test strategy

### 10.1 Unit tests

Mandatory cases:

- JWT expiry parsing;
- concurrent token refresh lock;
- GraphQL HTTP 200 with errors;
- partial data plus errors;
- error-code classification;
- invalid JSON and content type;
- timezone-aware parsing;
- JST day/month boundaries;
- year transition and leap day;
- duplicate interval merge;
- corrected value with same interval;
- opaque/non-numeric versions;
- missing and null fields;
- import/export separation;
- deterministic cumulative totals;
- stale-data calculation;
- storage migration.

### 10.2 HA integration tests

- successful config flow;
- multiple-account selection;
- duplicate account abort;
- invalid authentication;
- cannot-connect handling;
- reauthentication;
- setup, unload, reload;
- coordinator transient failure and recovery;
- entity availability and state classes;
- diagnostics redaction;
- options updates;
- config-entry migration.

### 10.3 Contract fixtures

Store sanitized official and real-account response shapes as JSON fixtures. Fixtures must cover both legacy and newer reading providers. No live credentials are used in pull-request CI.

A separately controlled scheduled compatibility check may be introduced later, using a dedicated low-risk test account and repository secrets, but must never expose personal customer data in logs or artifacts.

## 11. Open-source project operating model

Required repository files:

```text
CONTRIBUTING.md
CODE_OF_CONDUCT.md
SECURITY.md
LICENSE
.github/CODEOWNERS
.github/ISSUE_TEMPLATE/bug_report.yml
.github/ISSUE_TEMPLATE/feature_request.yml
.github/pull_request_template.md
```

Required CI:

- Ruff lint and format;
- type checking;
- pytest with coverage threshold;
- hassfest;
- HACS validation;
- manifest and translation validation;
- dependency review;
- CodeQL where appropriate;
- Dependabot or Renovate.

Contribution boundaries:

- GraphQL changes require fixtures and parser tests;
- new sensors require a documented source and state semantics;
- inferred values must be labeled and disabled by default unless broadly reliable;
- new persistent fields require a storage migration;
- diagnostics changes require redaction tests;
- user-facing strings require English, Japanese, and Korean updates or clearly documented fallback behavior.

Release policy:

- Semantic Versioning;
- pre-1.0 releases may change storage and entities only through explicit migrations;
- release notes must list entity additions/removals, storage migration, GraphQL changes, and reauthentication requirements;
- releases should be signed or produced by a protected GitHub Actions workflow;
- `main` remains releasable and protected by required checks.

## 12. Concrete implementation roadmap

### Phase 0 — Repository foundation

- normalize standard custom-component layout;
- add license, governance, issue forms, CI, test harness;
- correct manifest links and codeowners;
- add architecture decision records.

Exit criterion: empty integration skeleton passes Ruff, pytest, hassfest, and HACS validation.

### Phase 1 — Typed API foundation

- GraphQL transport;
- structured error parser;
- token manager;
- typed account discovery;
- sanitized fixtures and tests.

Exit criterion: authentication and account listing are fully fixture-tested and config flow distinguishes auth/connect/rate-limit failures.

### Phase 2 — Discovery and legacy readings

- account/property/supply-point models;
- explicit config-flow selection;
- legacy reading provider;
- normalized `EnergyReading` output.

Exit criterion: no production code indexes account/property/supply-point lists at zero without an explicit single-item assertion.

### Phase 3 — Persistent ledger and reconciliation

- versioned HA Store schema;
- interval merge and correction logic;
- overlap polling;
- daily reconciliation;
- migration and corruption recovery tests.

Exit criterion: restart, duplicate data, corrected data, and missing-late data produce deterministic aggregates.

### Phase 4 — Core entities

- supply-point devices;
- latest/today/yesterday/month/previous-month;
- deterministic cumulative energy;
- timestamp, delay, and freshness;
- diagnostics and redaction.

Exit criterion: Energy Dashboard accepts the cumulative sensor without synthetic incremental drift.

### Phase 5 — New supply-point readings adapter

- inspect current authenticated schema;
- implement current `SupplyPointType.readings` adapter;
- support units, device/register, direction, and quality where available;
- automatic provider selection with observable fallback.

Exit criterion: both providers pass the same domain contract tests.

### Phase 6 — Tariff and cost

- agreements and tariff discovery;
- official billing values where accessible;
- tariff-derived estimate only when all required components are known;
- optional flat-rate estimate with explicit naming.

Exit criterion: no monetary entity is presented as authoritative unless its source is authoritative.

### Phase 7 — Public release hardening

- Japanese, English, Korean documentation;
- installation and migration guides;
- beta feedback issue template;
- release automation;
- HACS publication readiness;
- security and privacy review.

Exit criterion: reproducible installation, diagnostics, tests, release artifacts, and contributor workflow.

## 13. Immediate decisions for this repository

The following are normative unless superseded by an ADR:

1. Domain remains `octopus_energy_japan`.
2. Account and supply-point discovery is explicit; index-zero shortcuts are prohibited.
3. `EnergyReading` is the central API-neutral model.
4. Readings are persisted in a versioned interval ledger.
5. Energy totals are deterministic ledger aggregates.
6. Authentication is managed by a token manager, not by each coordinator refresh.
7. GraphQL errors are classified from structured extensions.
8. Legacy and current reading APIs are provider adapters.
9. Delayed average power and flat-rate cost are inferred, clearly named, and disabled by default.
10. Sensitive OEJP identifiers are excluded from entity states and normal attributes.
11. Every GraphQL operation requires sanitized fixtures and tests.
12. Every storage change requires migration tests.
13. CI and contributor governance are part of the product, not post-release cleanup.

## 14. Final conclusion

`mapplebox/oejp` demonstrates the feature set users want, but its aggregation, cumulative accounting, cost semantics, packaging, and multi-account assumptions are not suitable for a production public integration.

`Shuangbing/oejp-hacs` is the better Home Assistant MVP and provides useful coordinator, entity-description, overlap-window, and freshness ideas. However, it still logs in with the password on every poll, classifies errors by strings, supports only first-object discovery, and persists only a synchronization cursor rather than a reading ledger.

`lvctr/hass-oejp` is not an OEJP implementation. Its value is limited to development and project-governance scaffolding inherited from the integration blueprint.

The differentiator for this project should not be a larger sensor list. It should be a reliable domain and persistence foundation: explicit account discovery, typed adapters for evolving OEJP schemas, structured error handling, secure token lifecycle, deterministic interval reconciliation, correct Home Assistant state semantics, complete tests, and a contributor-friendly repository. Once that foundation exists, tariff, billing, export, meter-quality, and future OEJP capabilities can be added without rewriting the integration.