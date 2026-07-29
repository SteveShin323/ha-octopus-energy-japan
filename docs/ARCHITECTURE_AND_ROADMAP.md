# OEJP Home Assistant Integration: Architecture, Comparative Review, and Roadmap

Status: design baseline  
Repository: `SteveShin323/ha-octopus-energy-japan`  
Domain: `octopus_energy_japan`  

## 1. Purpose

This document defines the technical and community direction for a production-quality, unofficial Home Assistant integration for Octopus Energy Japan (OEJP).

The design is based on four source groups:

1. The official OEJP GraphQL API documentation and changelog.
2. The official `octoenergy/oejp-api-example` reference implementation.
3. `mapplebox/oejp`.
4. `Shuangbing/oejp-hacs`.
5. `lvctr/hass-oejp`.

The goal is not merely to duplicate the existing integrations. The project should provide a more correct, testable, extensible, observable, and contributor-friendly implementation while minimizing avoidable API traffic and protecting user credentials.

---

## 2. Executive conclusion

The three reviewed repositories offer useful ideas but none is a sufficient long-term base.

| Repository | Actual role | Main strength | Main limitation |
|---|---|---|---|
| `mapplebox/oejp` | Feature-oriented HA integration | Broad set of consumption, power, cost, and cumulative sensors | Several values are locally inferred rather than authoritative; architecture and test coverage are limited |
| `Shuangbing/oejp-hacs` | Small HACS integration with persistent delayed-reading cache | Clear async client/coordinator separation and explicit delayed-data handling | Re-authenticates with email/password on every poll, selects only the first account/property/supply point, and has no robust API error taxonomy |
| `lvctr/hass-oejp` | Unmodified or minimally modified integration blueprint | Strong project scaffolding, CI, issue templates, devcontainer, contributor structure | It is not an OEJP implementation; manifest and README still identify the integration blueprint |

The proposed project should combine:

- the user-facing breadth demonstrated by `mapplebox/oejp`;
- the storage and coordinator ideas demonstrated by `Shuangbing/oejp-hacs`;
- the repository hygiene and contributor tooling present in the integration blueprint used by `lvctr/hass-oejp`;
- a stricter API/domain model based on current OEJP GraphQL documentation rather than only the legacy sample query.

The core design decision is to build an API-neutral domain layer around `Account`, `SupplyPoint`, `EnergyReading`, `Agreement`, and `Tariff`, with adapters for both the legacy `halfHourlyReadings` path and the newer `SupplyPointType.readings` model.

---

## 3. Official OEJP API constraints that drive the design

### 3.1 Endpoint and transport

GraphQL requests are sent to:

```text
https://api.oejp-kraken.energy/v1/graphql/
```

The API uses HTTPS POST requests. GraphQL failures can be returned with HTTP 200, so transport success and operation success must be handled separately.

Every response must be evaluated in this order:

1. Network and timeout failure.
2. HTTP status and content validity.
3. JSON decoding.
4. GraphQL `errors` array.
5. Presence and validity of `data`.
6. Schema-specific parsing.

### 3.2 Authentication

The official example obtains a JWT through `obtainKrakenToken` using email and password. The response can include an access token, refresh token, expiry information, and JWT payload.

The integration must not assume that one access token remains valid indefinitely. It also should not log in with the password on every ordinary data refresh unless the API provides no viable refresh flow.

Required strategy:

1. Authenticate during config flow.
2. Keep the short-lived access token in memory.
3. Decode or record its expiration safely.
4. Refresh shortly before expiry when the supported refresh mutation is confirmed.
5. Retry a failed authenticated operation once after token renewal.
6. Trigger Home Assistant reauthentication only when credentials or refresh credentials are truly invalid.

Until refresh-token behavior is verified against a real OEJP account, the implementation may temporarily reacquire a token after expiry, but it must not reacquire it on every polling cycle.

### 3.3 GraphQL errors

The client must parse `errors[].extensions`, especially:

- `errorType`
- `errorCode`
- `errorDescription`
- `path`

The public error examples include authorization codes such as `KT-CT-1112`. Complexity, node, and request-rate limits have their own codes such as `KT-CT-1188`, `KT-CT-1189`, and `KT-CT-1199`.

The integration should expose stable Python exceptions rather than leaking raw response dictionaries:

```python
class OejpError(Exception): ...
class OejpTransportError(OejpError): ...
class OejpInvalidResponseError(OejpError): ...
class OejpGraphQLError(OejpError): ...
class OejpAuthenticationError(OejpGraphQLError): ...
class OejpAuthorizationError(OejpGraphQLError): ...
class OejpRateLimitError(OejpGraphQLError): ...
class OejpQueryValidationError(OejpGraphQLError): ...
class OejpNotFoundError(OejpGraphQLError): ...
```

Each exception should preserve sanitized structured error metadata for diagnostics.

### 3.4 Query complexity and rate limiting

The official documentation specifies query-complexity and hourly point limits. This makes a single oversized “fetch everything” operation a poor design.

The project should separate data by refresh cadence:

- Consumption readings: normally every 30–60 minutes.
- Account and supply-point metadata: every 12–24 hours.
- Agreement and tariff metadata: every 12–24 hours.
- Billing data: every 6–24 hours, only when implemented.
- Historical reconciliation: once daily or on explicit service request.

Queries should request only fields required by the current feature.

### 3.5 Schema evolution

The OEJP changelog shows frequent additions, removals, nullability changes, and deprecations. Important reading-related changes include introduction and subsequent restructuring of `SupplyPointType.readings`, device/register reading access, units filtering, optional granularity, and quality metadata.

Consequences:

- GraphQL query strings must be isolated from Home Assistant entity code.
- API response parsing must be covered by fixture-based tests.
- Legacy and current reading APIs should be adapters into one internal model.
- Deprecated GraphQL fields must be tracked explicitly.
- CI should include a scheduled schema-compatibility check when a safe authenticated test setup becomes available.

---

## 4. Review: `mapplebox/oejp`

### 4.1 Observed design

The repository describes an async Home Assistant integration exposing:

- latest half-hour consumption;
- power estimated from a 30-minute energy interval;
- today and yesterday consumption;
- month-to-date and previous-month consumption;
- local cost estimates based on a user-configured yen-per-kWh value;
- a restored cumulative `total_increasing` energy sensor;
- token recovery behavior;
- Japan time-zone handling.

The implementation uses the legacy GraphQL path:

```text
viewer.accounts
account.properties.electricitySupplyPoints.halfHourlyReadings
```

It parses reading values with `Decimal`, treats `version` as a string, and calculates calendar aggregates in JST.

### 4.2 Strengths

1. **Feature discovery**  
   It demonstrates which entities are immediately useful to Home Assistant users: latest interval, daily/monthly totals, estimated power, cost, and an Energy Dashboard-compatible sensor.

2. **Use of `Decimal` at the API boundary**  
   Electricity values and monetary values should not initially be parsed as binary floating-point values.

3. **JST-aware aggregation**  
   Day and month boundaries are calculated in Japan time instead of assuming UTC.

4. **Options flow**  
   User-configurable values are separated from initial credentials.

5. **Awareness of Energy Dashboard requirements**  
   It recognizes that interval readings and cumulative energy require different Home Assistant state semantics.

6. **Recent-reading window**  
   A small recent query for the latest value is more efficient than repeatedly loading an entire month solely to determine the most recent interval.

### 4.3 Limitations and correctness risks

1. **Estimated power is not current power**  
   Converting a 30-minute energy reading to watts with `kWh × 2000` yields average power over that completed interval. Labeling it as current power can mislead users, especially because OEJP data is delayed. The proposed project may expose this only as an explicitly named diagnostic entity such as “Average power for latest reported interval,” disabled by default.

2. **Locally estimated cost is incomplete**  
   A flat yen-per-kWh calculation does not reproduce Japanese billing, which can include base charges, tiered rates, fuel-cost adjustment, renewable-energy levy, discounts, tax, and rounding. Such a sensor must be clearly named “simple usage estimate,” never presented as the OEJP bill or official cost.

3. **Synthetic cumulative sensor can drift or double count**  
   A `RestoreEntity` that increments from the latest reading is vulnerable to duplicate intervals, delayed corrections, missed intervals, database restore anomalies, and reading-version updates. The cumulative series should instead be computed deterministically from a persisted interval ledger or a stable API cumulative source.

4. **First-object assumptions**  
   Existing implementations commonly pick the first account, property, and supply point. That fails for moved customers, closed accounts, multiple homes, and import/export combinations.

5. **Broad monthly re-query cost**  
   Re-querying previous month through current time during every refresh is unnecessary. A persisted reading store plus overlap reconciliation is more efficient and more accurate.

6. **Limited separation of concerns**  
   Derived sensor calculation, API transport, persistence, and Home Assistant entity behavior should be more clearly separated.

7. **No demonstrated comprehensive automated tests**  
   The high-risk logic—month boundaries, correction versions, DST-independent JST handling, duplicate reads, and restore behavior—needs unit and integration tests.

### 4.4 What to reuse conceptually

- Sensor feature inventory.
- `Decimal` parsing.
- Explicit JST boundary calculations.
- Options flow pattern.
- Separate recent and historical data windows.

### 4.5 What not to copy directly

- “Current power” naming for delayed interval averages.
- Flat cost as an authoritative monetary sensor.
- Incremental `RestoreEntity` cumulative accounting without a reading ledger.
- First account/property/supply-point selection.

---

## 5. Review: `Shuangbing/oejp-hacs`

### 5.1 Observed design

This repository has a compact, recognizable Home Assistant structure:

```text
custom_components/octoenergy_jp/
├── __init__.py
├── api.py
├── config_flow.py
├── const.py
├── coordinator.py
├── manifest.json
├── models.py
├── sensor.py
├── strings.json
└── translations/
```

The client is asynchronous and uses Home Assistant's shared `aiohttp` session. The coordinator handles delayed readings and uses Home Assistant storage. The config flow exposes API URL, scan interval, and synchronization range. The repository exposes latest half-hour usage, today's total, and latest data timestamp.

### 5.2 Strengths

1. **Good minimum module separation**  
   Transport, models, coordinator, config flow, and sensors are separated instead of being placed in one file.

2. **Home Assistant shared HTTP session**  
   Correct use of `async_get_clientsession` avoids creating unnecessary sessions.

3. **Persistent delayed-reading cache**  
   A `Store`-backed ledger is the right direction for an API where readings arrive late and may be revised.

4. **Configurable polling and synchronization window**  
   This supports real-world tuning while enforcing minimum and maximum limits.

5. **Overlap-based reconciliation**  
   Re-fetching a recent overlap window is preferable to requesting the entire history every cycle.

6. **Explicit source timestamps**  
   Surfacing original `startAt` and `endAt` helps users understand delayed data.

7. **Config entry lifecycle**  
   It supports setup, unload, update listener, and a migration hook.

### 5.3 Limitations and correctness risks

1. **Fresh password login on every poll**  
   The latest change intentionally requests a new token on every coordinator update because the JWT expires quickly. This is operationally simple but inefficient, increases credential exposure, can trigger credential-stuffing defenses or login-specific rate limits, and ignores the refresh-token design exposed by the API.

2. **Authentication error detection by string matching**  
   Looking for words such as “invalid” or “credential” in the rendered error text is fragile. Classification must use `extensions.errorCode` and `extensions.errorType`.

3. **First account/property/supply point only**  
   The client indexes `[0]` at each level. This is not acceptable for a public integration.

4. **Email as the only config-entry unique ID**  
   One email can expose several accounts and supply points. The unique ID should ultimately be based on the selected account and supply point, or one account entry should own multiple discovered devices.

5. **Credentials retained as ordinary config-entry data**  
   Home Assistant custom integrations commonly store credentials in config entries, but refresh-token use should reduce repeated handling of the password. Diagnostics and logs must redact all credentials and account identifiers.

6. **Legacy reading path only**  
   The integration does not abstract the newer `SupplyPointType.readings` API.

7. **Limited entity model**  
   It is a good MVP but lacks yesterday/month/monthly reconciliation, account and supply-point devices, data-quality diagnostics, export readings, tariff metadata, and robust Energy Dashboard semantics.

8. **No clear test suite or CI evidence**  
   Persistence and overlap reconciliation especially require deterministic tests.

### 5.4 What to reuse conceptually

- Shared HA HTTP session.
- Coordinator and runtime-data object.
- Persistent reading storage.
- Overlap reconciliation.
- Source timestamp attributes.
- Configurable sync range with safe bounds.

### 5.5 What to replace

- Per-poll password login with token lifecycle management.
- String-based error classification with structured GraphQL error parsing.
- Index-zero account/property/supply-point selection with explicit discovery.
- Raw legacy API response assumptions with adapters and typed parsing.

---

## 6. Review: `lvctr/hass-oejp`

### 6.1 Actual state

Despite the repository name, the inspected initial commit and current files are essentially the `ludeeus/integration_blueprint` template. The README states that it is a blueprint rather than an end-user component, and the manifest still uses:

```json
{
  "domain": "integration_blueprint",
  "name": "Integration blueprint",
  "codeowners": ["@ludeeus"]
}
```

It therefore should not be treated as a competing OEJP implementation.

### 6.2 Strengths of the scaffold

The repository does contain several practices that our project should adopt:

- devcontainer-based Home Assistant development environment;
- Ruff linting and formatting;
- hassfest validation;
- HACS validation;
- Dependabot configuration;
- structured bug and feature request templates;
- contributor guidelines;
- release-oriented repository layout;
- explicit licensing.

### 6.3 Limitations

- No OEJP API client.
- No OEJP GraphQL queries.
- No OEJP config flow or sensors.
- Template names, ownership, links, and documentation remain unchanged.
- No domain-specific tests.

### 6.4 What to reuse conceptually

The repository is valuable only as a project-governance and CI checklist, not as runtime implementation guidance.

---

## 7. Proposed architecture

### 7.1 Repository structure

```text
.
├── .devcontainer/
├── .github/
│   ├── ISSUE_TEMPLATE/
│   ├── workflows/
│   ├── CODEOWNERS
│   └── pull_request_template.md
├── custom_components/
│   └── octopus_energy_japan/
│       ├── __init__.py
│       ├── api/
│       │   ├── __init__.py
│       │   ├── client.py
│       │   ├── auth.py
│       │   ├── errors.py
│       │   ├── queries.py
│       │   ├── legacy_readings.py
│       │   └── supply_point_readings.py
│       ├── config_flow.py
│       ├── const.py
│       ├── coordinator.py
│       ├── diagnostics.py
│       ├── entity.py
│       ├── models.py
│       ├── reading_store.py
│       ├── sensor.py
│       ├── strings.json
│       └── translations/
├── docs/
├── tests/
│   ├── fixtures/
│   ├── test_api.py
│   ├── test_auth.py
│   ├── test_config_flow.py
│   ├── test_coordinator.py
│   ├── test_reading_store.py
│   ├── test_sensor.py
│   └── test_diagnostics.py
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── hacs.json
└── pyproject.toml
```

### 7.2 Layer boundaries

#### API transport layer

Responsibilities:

- HTTP requests and timeout policy.
- GraphQL envelope parsing.
- structured exceptions.
- token lifecycle.
- no Home Assistant entities or aggregation logic.

#### OEJP domain layer

Typed immutable models:

```python
@dataclass(frozen=True, slots=True)
class OejpAccount:
    number: str
    status: str | None
    has_active_agreement: bool | None

@dataclass(frozen=True, slots=True)
class OejpSupplyPoint:
    identifier: str
    account_number: str
    property_id: str | None
    direction: Literal["import", "export", "unknown"]

@dataclass(frozen=True, slots=True)
class EnergyReading:
    supply_point_id: str
    start_at: datetime
    end_at: datetime
    value: Decimal
    unit: str
    direction: Literal["import", "export", "unknown"]
    version: str | None
    qualities: tuple[str, ...]
```

#### Reading persistence layer

Responsibilities:

- interval-keyed storage;
- deduplication;
- correction/version replacement;
- bounded retention;
- migration of stored schema;
- deterministic aggregates.

Logical primary key:

```text
supply_point_id + direction + start_at + end_at + unit
```

`version` is metadata used to decide replacement, not part of the interval identity.

If version ordering cannot be guaranteed, the newest API response replaces the stored interval while retaining a diagnostic change count.

#### Coordinator layer

Responsibilities:

- schedule updates;
- select overlap and backfill windows;
- combine account metadata and reading store;
- translate API exceptions to `ConfigEntryAuthFailed`, `UpdateFailed`, or availability state;
- avoid sensor-specific calculations where possible.

#### Entity layer

Responsibilities:

- map stable domain values to HA device classes and state classes;
- use translation keys;
- provide stable unique IDs;
- expose only safe attributes;
- group entities under account/supply-point devices.

---

## 8. Account and supply-point discovery

The config flow must not silently select `[0]`.

Recommended flow:

1. User enters email/password.
2. Integration authenticates and loads all viewer accounts.
3. If multiple eligible accounts exist, show an account selector.
4. Load all properties and electricity supply points for the selected account.
5. Show supply-point selector when there is more than one.
6. Identify import/export direction where available.
7. Create one config entry for an account, with discovered supply points represented as devices, unless real-world testing shows that per-supply-point entries are more maintainable.

Preferred unique ID:

```text
oejp:<account-number>
```

Supply-point entities use:

```text
<account-number>:<supply-point-id>:<entity-key>
```

The email address must not be the entity or device identity.

---

## 9. Reading API compatibility strategy

Define a protocol:

```python
class ReadingProvider(Protocol):
    async def async_get_readings(
        self,
        supply_point: OejpSupplyPoint,
        start_at: datetime,
        end_at: datetime,
    ) -> list[EnergyReading]: ...
```

Implement:

- `LegacyHalfHourlyReadingProvider`
- `SupplyPointReadingProvider`

Selection policy:

1. Prefer the current `SupplyPointType.readings` API when available to the authenticated customer and proven stable.
2. Fall back to the legacy `halfHourlyReadings` path.
3. Record the selected provider in diagnostics.
4. Do not change provider automatically on every refresh; cache the capability result and retry capability discovery after upgrade or a long interval.

This avoids coupling all entities to one generation of the GraphQL schema.

---

## 10. Reading synchronization algorithm

### Initial synchronization

- Default history: 35 days, sufficient for current and recent daily/monthly views.
- Allow bounded user-selected history only when justified.
- Split large ranges into smaller windows to reduce complexity and response risk.

### Normal refresh

- Poll every 30 or 60 minutes.
- Query from `latest_known_end - overlap` to now.
- Default overlap: 48 hours, because delayed revisions may not be limited to six hours.
- Upsert each interval into persistent storage.

### Daily reconciliation

- Once per day, re-fetch a longer bounded period, for example the current month plus seven days of the previous month.
- Reconcile changed values and versions.

### Manual backfill service

A later release may expose a safe service:

```text
octopus_energy_japan.backfill_readings
```

with start/end constraints and explicit confirmation through documentation. It must not permit unbounded historical queries.

---

## 11. Entity design

### Enabled by default

Per import supply point:

- Latest reported interval energy (`kWh`, measurement-like interval value).
- Today consumption (`kWh`, total).
- Yesterday consumption (`kWh`, total).
- Month-to-date consumption (`kWh`, total).
- Previous calendar month consumption (`kWh`, total).
- Latest reading end timestamp (`timestamp`).
- Data age (`duration`, diagnostic category).
- Data availability/currentness binary sensor or diagnostic sensor.

### Energy Dashboard source

Do not create a naïve restored counter that increments blindly.

Preferred options, in order:

1. An authoritative cumulative meter value from OEJP, if the API exposes one suitable for Home Assistant statistics.
2. A deterministic cumulative value derived from the persisted interval ledger with an explicit stable baseline.
3. A statistics import/backfill strategy only if Home Assistant APIs support it safely and it is tested.

The cumulative entity must remain monotonic across normal updates while correctly handling reading revisions. Any baseline reset must be explicit and documented.

### Disabled by default

- Average power over latest reported interval.
- Simple flat-rate cost estimate.
- Reading quality/debug sensors.
- API rate-limit usage.

### Monetary entities

Separate concepts:

- **Official API cost/bill**: value supplied by OEJP.
- **Tariff-derived estimate**: calculation using known tariff components.
- **Simple usage estimate**: `kWh × user-entered JPY/kWh`.

These must never share an ambiguous entity name.

---

## 12. Authentication and credential policy

- Never include email, password, access token, refresh token, account number, supply-point identifier, address, or billing name in logs.
- Diagnostics must redact or hash identifiers.
- Access tokens remain in memory.
- Refresh tokens may be stored in the config entry only after the supported refresh flow is verified.
- Password changes and invalid refresh credentials trigger an HA reauthentication flow.
- Temporary API failures do not trigger reauthentication.
- Authentication requests use a lock so simultaneous coordinator calls cannot start multiple logins.
- Retry only once after refreshing a token.

---

## 13. Diagnostics and observability

Add `diagnostics.py` early, not after users report failures.

Safe diagnostics should include:

- integration version;
- selected reading-provider type;
- account count and supply-point count, not raw identifiers;
- last successful metadata refresh;
- last successful readings refresh;
- latest source reading timestamp;
- reading-store count and oldest/newest timestamps;
- configured polling and overlap windows;
- last sanitized OEJP error code/type;
- token expiry timestamp, never token value;
- number of revised intervals detected.

Debug logs should describe operation and counts rather than payload contents.

---

## 14. Testing strategy

### Unit tests

- JWT expiry parsing.
- GraphQL error classification by code/type.
- datetime parsing with `Z` and explicit offsets.
- JST day/month boundaries.
- duplicate reading upsert.
- reading revision replacement.
- missing intervals.
- multiple accounts/properties/supply points.
- import/export separation.
- null and malformed fields.
- cost-label semantics.

### Config flow tests

- valid login with one account.
- valid login with multiple accounts.
- multiple supply points.
- invalid credentials.
- transient API failure.
- duplicate account entry.
- reauthentication.
- options updates.

### Coordinator tests

- first refresh.
- overlap refresh.
- token expiry and one retry.
- rate limit handling.
- persistent-store reload.
- daily reconciliation.

### Entity tests

- device and state classes.
- units.
- availability.
- unique IDs.
- disabled-by-default entities.
- data age and timestamps.

### Contract fixtures

Store sanitized GraphQL responses from:

- official example legacy reading query;
- multiple-account response;
- multiple-property response;
- current supply-point readings API;
- revised readings;
- quality metadata;
- authentication and authorization errors;
- rate-limit error;
- partial GraphQL data with errors.

No real credentials or identifiers may enter the repository.

---

## 15. CI and project quality

Required workflows:

1. Ruff lint and formatting.
2. Pytest with coverage.
3. mypy or strict-enough static checking for the API/domain modules.
4. hassfest validation.
5. HACS validation.
6. JSON and translation validation.
7. Dependabot or Renovate.
8. Release workflow producing a HACS-compatible ZIP.
9. Scheduled dependency and compatibility test.

Recommended merge policy:

- pull requests required;
- squash merge;
- CI required;
- at least one approving review after the contributor base grows;
- no direct commits to `main` except initial bootstrap and urgent maintainer recovery.

---

## 16. Open-source governance

Files to add:

- `CONTRIBUTING.md`
- `CODE_OF_CONDUCT.md`
- `SECURITY.md`
- `.github/CODEOWNERS`
- issue templates for bugs, API-schema changes, and feature requests;
- pull request template;
- architectural decision record directory (`docs/adr/`).

Contribution rules:

- Each behavior change requires tests.
- GraphQL schema changes require a sanitized fixture and documentation update.
- New entities require a clear source-of-truth statement, unit, device class, state class, default-enabled decision, and unique-ID plan.
- Derived monetary and power values require unambiguous naming.
- Public API methods and domain models require type annotations.
- Contributor-facing documentation should be written in English; user documentation should ultimately support Japanese and English, with Korean translations welcome.

Use semantic versioning:

- `0.x`: API and entity model may change with migration support.
- `1.0`: stable config-entry, device, entity unique IDs, and reading-store schema.

---

## 17. Implementation roadmap

### Phase 0 — repository foundation

- Complete license and contributor files.
- Add devcontainer, Ruff, pytest, hassfest, and HACS CI.
- Add architecture ADRs.
- Correct manifest metadata and translations.

Exit criteria: empty/minimal integration passes all CI checks.

### Phase 1 — robust API client

- Implement transport and structured GraphQL errors.
- Implement token manager.
- Implement account discovery.
- Implement legacy reading provider.
- Add sanitized fixtures and unit tests.

Exit criteria: client can authenticate, enumerate accounts and supply points, and retrieve readings without HA entity code.

### Phase 2 — config flow and devices

- Multi-account selection.
- Multi-supply-point discovery.
- Stable config-entry and device identifiers.
- Reauthentication flow.

Exit criteria: common and multi-account scenarios are tested.

### Phase 3 — persisted readings and core sensors

- Versioned reading store.
- Overlap sync and daily reconciliation.
- Core daily/monthly/timestamp/data-age entities.
- Diagnostics.

Exit criteria: restart, duplicate, delayed, revised, and missing-reading tests pass.

### Phase 4 — Energy Dashboard

- Determine authoritative or deterministic cumulative strategy.
- Validate long-term statistics behavior.
- Document correction handling.

Exit criteria: no double counting across restart/revision tests.

### Phase 5 — current supply-point readings API

- Implement newer reading provider.
- Compare output with legacy provider.
- Add capability selection and fallback.
- Support import/export when exposed.

Exit criteria: provider parity tests and safe migration behavior.

### Phase 6 — tariff and billing

- Expose agreements and tariff metadata.
- Add official billing values when available.
- Add derived estimates only with complete labeling and test fixtures.

Exit criteria: official and estimated values are technically and visually distinguishable.

### Phase 7 — public release quality

- Beta feedback cycle.
- Japanese and English documentation.
- Release notes and migration guides.
- HACS default repository submission when maturity permits.

---

## 18. Immediate decisions for this repository

The following decisions should be treated as the initial baseline:

1. Domain remains `octopus_energy_japan`.
2. The integration uses Home Assistant's shared `aiohttp` session.
3. API transport and HA entities are separate modules.
4. `Decimal` is used for API energy and monetary parsing.
5. All calendar aggregation uses `Asia/Tokyo`; internal timestamps remain timezone-aware.
6. No `[0]` selection is allowed without first proving that only one object exists.
7. A persistent interval ledger is required before an Energy Dashboard cumulative sensor is released.
8. Average interval power and flat-rate costs are disabled by default and named as estimates.
9. Structured GraphQL error codes are the source of error classification.
10. Both legacy and current reading APIs are hidden behind a provider interface.
11. Tests and diagnostics are part of the MVP, not post-release enhancements.
12. Public contribution tooling and governance are established before broad feature expansion.

---

## 19. Source references

- OEJP API documentation: `https://docs.oejp-kraken.energy/`
- OEJP GraphQL basics: `https://docs.oejp-kraken.energy/graphql/guides/basics/`
- OEJP GraphQL changelog: `https://docs.oejp-kraken.energy/graphql/changelog/`
- Official example: `https://github.com/octoenergy/oejp-api-example`
- Feature-oriented implementation: `https://github.com/mapplebox/oejp`
- Delayed-reading HACS implementation: `https://github.com/Shuangbing/oejp-hacs`
- Blueprint-based repository: `https://github.com/lvctr/hass-oejp`

Inspected notable commits:

- `mapplebox/oejp@3715649be89eb2972e52f3130c5b157cd09a089a`
- `Shuangbing/oejp-hacs@66afe69d21d5834efa3f022a949a7e12c88f414d`
- `Shuangbing/oejp-hacs@135da3c26f198a7e3429d17220b91608ca36b070`
- `lvctr/hass-oejp@d9c675c286e6c1f0b1328aa56455fb2e825ba177`
