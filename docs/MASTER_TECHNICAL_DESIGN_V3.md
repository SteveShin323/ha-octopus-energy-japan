# OEJP Home Assistant Integration — Master Technical Design v3

Status: normative implementation plan
Reviewed: 2026-07-29
Repository: `SteveShin323/ha-octopus-energy-japan`
Domain: `octopus_energy_japan`

## 1. Authority and objectives

This document is the normative architecture and delivery plan for the project. It
supersedes the archived architecture and comparative-review documents when they
conflict. Decisions that need a durable rationale are also recorded in
`docs/adr/`.

The design is informed by:

- the official OEJP GraphQL guides, reference, changelog, and API example;
- `strongbugman/ha-octopusenergy-oejp`;
- `Shuangbing/oejp-hacs`;
- `mapplebox/oejp`; and
- `lvctr/hass-oejp`.

The project will provide:

- a high-quality, read-only Home Assistant integration for OEJP;
- HACS-first distribution with architecture suitable for a future Home Assistant
  Core proposal;
- correct handling of multiple accounts, supply points, meters, registers, and
  import/export series;
- deterministic results after delayed, duplicated, corrected, or reordered
  readings and Home Assistant restarts;
- contributor-friendly English documentation and user-facing Japanese
  documentation; and
- privacy-preserving diagnostics without external telemetry.

Release quality gates are:

- all required GitHub checks pass;
- line and branch coverage is at least 95%;
- authentication, ledger, statistics, and migration code reaches 100% coverage;
- no known P0 or P1 defects;
- all supported config-entry lifecycle paths are exercised with the Home
  Assistant test harness; and
- release candidates pass a real OEJP account test matrix and clean HACS
  install, upgrade, and removal tests.

The project targets Home Assistant Gold quality requirements before beta and
Platinum-oriented async, typing, and efficiency practices for 1.0.

## 2. Authentication

### 2.1 Public integration behavior

The public integration must not collect or retain an OEJP password. It uses the
OEJP authorization server:

```text
Add integration
  -> OEJP-hosted sign-in
  -> Authorization Code with PKCE S256
  -> access and refresh tokens
  -> GraphQL requests
  -> automatic refresh
  -> Home Assistant reauthentication after revoke or terminal refresh failure
```

Authorization Code with PKCE is the primary flow. Device Authorization Grant is
an optional fallback for environments where a browser redirect cannot be
completed. A client secret is never embedded in the repository or distributed
integration.

The shared public client ID may be committed only after OEJP confirms that a
single published client ID may be used by multiple Home Assistant installations.
If OEJP requires a user-provided client ID, the integration will use Home
Assistant Application Credentials instead.

The OAuth access and refresh tokens remain in the user's Home Assistant config
entry. They must not appear in logs, entity states, diagnostics, fixtures, or
issue templates. The GraphQL authorization scheme is implemented only after
OEJP confirms it or an authorized local probe verifies it.

### 2.2 Authentication abstraction

```python
class AuthSession(Protocol):
    async def async_get_authorization_header(self) -> str: ...
    async def async_refresh(self) -> None: ...
    async def async_revoke(self) -> None: ...
```

Implementations:

- `OejpPkceAuthSession`;
- `OejpDeviceAuthSession`;
- `FakeAuthSession` for deterministic tests; and
- `LegacyKrakenAuthSession`, isolated to local read-only API probes.

The deprecated email/password Kraken operation is removed from the public config
flow and runtime before alpha. It may remain temporarily in probe-only code while
the OAuth application is pending.

### 2.3 OAuth application outcomes

The implementation responds to OEJP's application decision as follows:

| OEJP response | Project response |
|---|---|
| Shared public client and PKCE approved | Ship PKCE as the default |
| PKCE and device grant approved | PKCE default, device flow fallback |
| Separate client IDs required | Register separate HA auth implementations |
| Device flow only | Make device flow the supported setup path |
| Client secret required | Do not ship it; renegotiate public-client terms |
| User-specific client ID required | Use Application Credentials |
| Broad customer scope only | Document impact and request least privilege |
| Application rejected | Do not publish a functional release; continue fixture-based development |

The response record must capture client IDs per grant, redirect URI, scopes and
GraphQL permissions, token lifetimes, refresh rotation behavior, authorization
scheme, generic/legacy reading access, billing access, and permission to publish
the client ID.

## 3. Config entries, identity, and privacy

One OAuth login identity owns one config entry. All accounts and supply points
visible to that login are managed by that entry.

The config-entry unique ID is:

```text
HMAC(local installation secret, issuer + OIDC subject)
```

Account and supply-point device identifiers use the same installation-local HMAC
construction over provider identifiers. This keeps identifiers stable within one
Home Assistant installation while preventing cross-installation correlation.

Raw provider identifiers may be stored locally when required to call the API and
join ledger records. The privacy document must state this explicitly. Raw
account numbers, supply-point identifiers, names, and addresses are excluded
from entity states and attributes, logs, diagnostics, and external telemetry.
Addresses are not used as device or entity names.

Active accounts and supply points are enabled automatically. Historical or
closed resources are discovered but disabled by default and can be selected
during reconfiguration. New supply points are added without creating a new
config entry. Removed or closed supply points remain unavailable rather than
being deleted, preserving statistics continuity.

The current pre-alpha account-per-entry format is not a compatibility contract.
Existing development installs must reconfigure before the first alpha. After the
first alpha, every config or storage format change requires a migration and
migration tests.

## 4. Architecture and type boundaries

```text
OAuth implementation / AuthSession
  -> GraphQL transport
  -> operations and strict parsers
  -> discovery and capability registry
  -> reading providers
  -> persistent interval ledger
  -> aggregation service
  -> external statistics projector
  -> cadence-specific coordinators
  -> Home Assistant devices and entities
  -> diagnostics and repairs
```

Raw GraphQL dictionaries do not cross a parser boundary. Coordinators and
entities consume typed domain models only.

The normalized `EnergyReading` contains:

- account, supply-point, device, and register identifiers;
- import/export direction;
- timezone-aware UTC start and end timestamps;
- `Decimal` value;
- unit and granularity;
- provider source and revision/version;
- quality metadata;
- provider-issued official cost, if present; and
- fetch timestamp.

`ReadingSeriesKey` identifies account, supply point, device/register, direction,
unit, and source. Other core types are `CapabilitySnapshot`, `LedgerRecord`,
`CorrectionResult`, `AggregationSnapshot`, `StatisticsProjection`, and
`OejpRuntimeData`.

## 5. GraphQL operations, providers, and errors

### 5.1 Reading providers

`GenericReadingsProvider` handles the current `SupplyPointType.readings` model,
including devices, registers, import/export, units, granularity, and quality.

`LegacyHalfHourlyProvider` handles `halfHourlyReadings` and
`intervalReadings`, preserving version and `costEstimate` where available.

The generic provider is preferred. Legacy fallback is allowed only for a
disabled/unavailable generic field, a supply point that is not configured for
the generic model, a known OAuth permission gap, or a recognized schema
capability mismatch.

Fallback is forbidden for authentication failures, rate limits, timeouts,
server failures, malformed responses, and invalid account or supply-point
identifiers. Those conditions must remain visible and must not be disguised as
provider incompatibility.

### 5.2 Strict and optional operations

Strict operations include authentication, viewer identity, core discovery, and
core readings. Their failure affects coordinator availability or triggers
reauthentication.

Optional operations include balance, billing, tariff, cost, and optional
device/register metadata. Partial data may be accepted only by an explicitly
optional execution path. Optional errors are recorded in capabilities and
redacted diagnostics without disabling consumption data.

Errors are normalized into:

- authentication;
- authorization;
- rate limit, complexity, or node limit;
- validation or schema change;
- not found;
- transient transport or server failure;
- invalid response; and
- optional-field partial failure.

Structured GraphQL codes, types, descriptions, and paths drive classification.
User-safe exception messages remain separate from redacted diagnostic details.

## 6. Persistent interval ledger

The ledger stores authoritative interval records, not only cumulative values or
the last synchronization cursor.

The logical key is:

```text
ReadingSeriesKey + UTC start_at + UTC end_at
```

Merge rules:

- identical interval and content is a no-op;
- value, version, quality, or official-cost changes are corrections;
- the provider snapshot returned for the requested range is authoritative for
  that fetch;
- conflicting duplicate intervals in one response are parser errors;
- correction count and fetch timestamps are retained; and
- aggregates and statistics can be rebuilt entirely from ledger data.

Storage uses versioned Home Assistant `Store` partitions by config entry,
supply point, and month. Current and previous month stay resident; older
partitions load lazily. Saves are atomic and debounced. Schema migrations are
explicit and tested. A damaged partition creates a repair issue without taking
down unrelated partitions or the entire integration. Damaged raw files and
tokens are never copied into diagnostics.

Property-based tests verify merge-order independence, idempotency, aggregation
sums, interval ordering, and correction projection.

## 7. Synchronization and scheduling

Default cadence:

| Data | Cadence |
|---|---:|
| Consumption | 30 minutes |
| Regular reading overlap | Most recent 72 hours |
| Initial backfill | Current and previous month |
| Query chunk | At most 7 days |
| Full recent reconciliation | Daily, current and previous month |
| Discovery | 24 hours |
| Agreement/contract/tariff | 12 hours |
| Billing | 12 hours |
| Optional long backfill | Up to 13 months |

Long backfills use a rate-aware queue. Rate-limit responses honor `Retry-After`
when supplied, otherwise exponential backoff with jitter. Repeated identical
errors are log-throttled. Startup calls are staggered so multiple coordinators
or entries do not create a request burst.

Ledger timestamps are UTC. Day, week, month, and year aggregates use
`Asia/Tokyo` boundaries.

## 8. Home Assistant devices and entities

Device hierarchy:

```text
OEJP account device
  -> electricity supply-point device
     -> meter/register/direction entity series
```

Meters and registers do not become separate devices unless the API exposes them
as independently manageable resources.

Enabled by default:

- latest reported interval consumption;
- today, yesterday, this week, this month, and last month consumption;
- latest reading timestamp;
- data delay;
- supply-point status; and
- data available.

Disabled by default:

- average power for the latest reported interval;
- reading quality and correction count;
- API rate-limit information;
- account balance;
- latest bill/payment summary; and
- official OEJP cost estimate.

Names must not say "live", "real-time", or "current power" for delayed interval
data. Average power is labeled as an interval average. Official provider cost is
distinguished from any estimate. Full reading arrays and financial histories are
not exposed as attributes. Device/state classes are finalized only after
recorder and long-term-statistics tests.

## 9. Energy Dashboard and statistics

External statistics are projected per supply point and direction:

- imported energy in kWh;
- exported energy in kWh; and
- official cost in JPY only when OEJP supplies it.

Projection produces hourly state and cumulative sum. When the ledger reports a
correction:

1. find the earliest changed interval;
2. identify the affected first hour;
3. load the preceding cumulative checkpoint;
4. recalculate hourly values from the changed hour to the present;
5. regenerate every affected cumulative sum; and
6. update using supported Home Assistant recorder statistics APIs.

Projection is idempotent. Restarts, duplicate fetches, and reprocessing the same
correction must yield the same statistics. Period aggregate sensors are not the
Energy Dashboard source of truth.

## 10. Account, contract, tariff, and billing

After consumption is stable, optional operations add:

- account status and balance;
- agreement and contract periods;
- product/tariff names and rate components;
- official `costEstimate`;
- latest bill/invoice amount and due date; and
- summarized payment/transaction information.

Complete bills or transaction histories are not stored in entity attributes.
Simple user-entered `kWh x rate` cost sensors are outside the 1.0 scope because
they cannot faithfully represent Japanese billing.

## 11. Diagnostics, repairs, and observability

Redacted diagnostics include integration and HA versions, selected providers and
capabilities, coordinator success/failure times, reading counts and ranges,
correction counts, ledger schema/partition health, projection status,
non-identifying rate-limit information, and redacted GraphQL code/type/path.

They exclude OAuth tokens, email, account and supply-point identifiers,
addresses, names, raw bills/transactions, and raw reading arrays.

Repairs cover revoked OAuth, missing auth implementation, missing required
scope, provider capability/schema changes, ledger migration or partition damage,
statistics drift, and prolonged lack of readings. The integration sends no
external telemetry.

## 12. Pull-request delivery sequence

1. **Design v3 and quality baseline** — this document and ADRs, archived prior
   designs, OAuth status record, legacy token-field correction, full-integration
   typing, 95% coverage gate, pinned Actions, security automation, and quality
   scale tracking.
2. **OAuth and AuthSession** — PKCE/Application Credentials, device-flow
   abstraction, refresh/rotation/revoke/reauth, mock-server tests, password-flow
   removal, and one-entry-per-login identity.
3. **Safe real-account probe** — read-only operations, automatic PII
   substitution, synthetic fixtures, secret/PII scanner, and contract
   provenance.
4. **Discovery and capabilities** — all accounts/properties/supply points,
   meters/devices/registers, pagination, active/historical treatment,
   reconfiguration, and devices.
5. **Reading providers** — generic and legacy models, import/export,
   unit/granularity/quality/version/cost, strict fallback, and fixture contracts.
6. **Ledger and aggregation** — partitioned storage, deduplication, corrections,
   migration/recovery, JST aggregation, and backfill/reconciliation.
7. **Runtime and entities** — typed runtime, cadence coordinators, lifecycle,
   entities, availability, and English/Japanese UI translations.
8. **Energy statistics** — import/export/cost projection, correction replay,
   recorder harness tests, and Energy Dashboard documentation.
9. **Contract, tariff, and billing** — account/agreement/tariff and optional
   official cost/billing with partial-permission behavior.
10. **Diagnostics and operational recovery** — redaction, repairs, drift/schema
    handling, and non-blocking API/OIDC change monitoring.
11. **Documentation, translation, and release** — English normative docs,
    Japanese user docs, community templates, release process, HACS artifact,
    checksum/attestation, and clean install/upgrade/removal validation.

Each PR is complete only after all required checks pass and actionable review
threads are resolved.

## 13. Test matrix

Automated tests cover:

- HTTP 200 responses containing GraphQL errors, partial responses, and malformed
  payloads;
- timeout, retry, rate limit, backoff, and offline authorization server;
- exact authentication/authorization classification;
- PKCE, refresh rotation, revoke, and reauthentication;
- multiple or duplicate accounts and resource addition/closure;
- pagination and capability detection;
- only permitted generic-to-legacy fallback cases;
- import/export, multiple units, nullable granularity, quality, and revisions;
- reordering, duplication, correction, omission, and delayed arrival;
- ledger restart, atomic save, corruption, migration, and JST boundaries;
- statistics correction and idempotency;
- setup, unload, reload, registries, reconfigure, options, and reauth;
- diagnostics redaction and English/Japanese translations; and
- clean HACS packaging.

CI stores no real OEJP credential. Authorized API validation is performed
locally with the redacting probe, and only synthetic sanitized fixtures enter
the repository.

## 14. Documentation policy

English is the normative source. English documentation includes the README,
architecture, API contracts, development, testing, fixture redaction,
contributing, security, privacy, release process, and ADRs.

Japanese documentation includes the README, installation, OAuth setup, entity
reference, Energy Dashboard setup, delayed-reading explanation, privacy,
troubleshooting, diagnostic issue submission, and local-data removal.

Japanese user documentation and English/Japanese UI translations are synchronized
for every public release. Repository documentation and translations are not
maintained in Korean.

## 15. Release gates

- `0.1.x alpha`: OAuth, discovery, probe, and basic readings;
- `0.5.x beta`: ledger, core entities, Energy Dashboard, and diagnostics;
- `0.8.x beta`: import/export, agreements, official cost, and billing; and
- `1.0.0`: migrations, security, documentation, translations, and all quality
  targets complete.

No public functional release is made before OEJP confirms OAuth client and
permission details.

The release candidate is tested with a real account for initial OAuth,
automatic refresh and rotation, restart recovery, multiple accounts and supply
points, generic/legacy readings, import/export, delayed/corrected readings,
partial permissions, diagnostic redaction, HACS lifecycle, and Energy Dashboard
display.
