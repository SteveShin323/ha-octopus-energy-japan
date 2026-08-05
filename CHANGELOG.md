# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Sign-in with email and password works. The two OAuth methods are implemented and need a
published client ID before they can be completed.

## [Unreleased]

## [0.8.1] - 2026-08-05

Migration handling, which the version scheme names as a condition for `1.0.0`. No user-visible
change: nothing in 0.8.0 is broken today, and this removes a hazard that a future schema
change would otherwise have created.

### Added

- migration handling, which the version scheme names as a condition for `1.0.0` and which
  did not exist. `async_migrate_entry` is defined: nothing needs migrating yet, but Home
  Assistant refuses to load an entry whose major version differs when no handler exists, so
  its absence — rather than the schema change — is what would have broken every entry at the
  next bump. An entry from a newer version is refused instead of being read with older code;
- a sync checkpoint that cannot be read is discarded and planning starts again, instead of
  raising. Raising the checkpoint's schema version would previously have left every
  installation permanently unable to synchronise, escapable only by deleting the entry and
  losing its history. A checkpoint is derived from the ledger, so discarding one costs
  re-reading those windows and loses no readings. `diagnostics` counts the discards.

## [0.8.0] - 2026-08-05

First release. Beta under this project's version scheme: everything the README describes is
implemented, covered by tests, and verified against a real account, but two of the three
sign-in methods cannot be completed until a client ID exists.

The entries below record what went into it.

### Added

- a choice of three sign-in methods at setup. **Email and password** works without an
  OAuth client ID, storing the credential because the provider's refresh token lasts
  seven days and renewing does not extend it. **Octopus Energy Japan account** and
  **device code** are OAuth and never see the password; the device code needs no
  redirect URI, so it does not require My Home Assistant and is the better of the two
  once a client ID exists. One OEJP login owns one config entry
  under either method, so an entry can be switched to OAuth in place — keeping its
  readings and statistics, and deleting the stored password. Removing such an entry
  deletes the local credential but cannot revoke the token, because Octopus Energy Japan does not let a
  customer invalidate a refresh token; it expires within seven days. See [ADR 0008](docs/adr/0008-password-authentication.md);
- read-only OEJP integration: OAuth with PKCE, account and supply-point discovery,
  generic and legacy reading providers with an explicit fallback policy;
- a persistent correction-aware interval ledger and Asia/Tokyo calendar aggregation;
- consumption, timestamp, delay, and lifecycle entities per supply point and
  direction, plus data-availability binary sensors;
- Energy Dashboard external statistics rebuilt from the ledger, so a reading corrected
  later also corrects every total that used it;
- optional account, contract, product, and billing summaries, with financial
  entities disabled by default;
- privacy-preserving diagnostics and four informational repair issues;
- icons for every entity, and English and Japanese translations;
- English and Japanese user documentation, a privacy statement, and a release
  process;
- a setup refusal when My Home Assistant is not enabled, because sign-in returns
  through `my.home-assistant.io`, the only redirect address submitted to OEJP for
  registration. Without it Home Assistant builds the instance's own callback URL
  and the user would meet the provider's unregistered-redirect error part-way
  through sign-in, with nothing naming this integration as the cause;
- **electricity cost on the Energy Dashboard.** The tariff is read from the agreement in
  force — the stepped prices with their kWh boundaries, the daily standing charge, the
  monthly fuel-cost adjustment and the annual renewable levy — and an hourly cost statistic
  is published for `stat_cost`. Nothing is entered by the user and no unit price is assumed.
  Home Assistant's own energy validator accepts it, which is asserted with a real recorder.
  Measured at 104% of one real bill, for the two reasons recorded in
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md);

### Corrected during development

Recorded because each was a real defect and the reasoning is worth keeping.

- documentation said to leave the Energy dashboard's cost field empty because a fixed
  price would be wrong. It is worse than wrong: Home Assistant builds a cost sensor only
  when the energy source is a valid entity id, and an external statistic is not one, so a
  price typed there is ignored rather than applied. The guides now say that, and a test
  pins the fact;
- device pages now show the account number and the supply-point number
  (供給地点特定番号) as their serial number. Hiding every provider identifier left a
  customer with two supply points unable to tell which device was which; entity IDs,
  names, states, attributes, and the diagnostics download still carry none;
- Energy Dashboard statistics were named after an identity digest, which is the only
  thing the Energy picker shows, so a household with two supply points could not tell
  them apart. They now take the supply-point device's name — `OEJP supply point 1-1
  Import energy` — and devices are created before the first refresh so the first
  publication already carries it. Home Assistant's own energy validator accepts both
  directions with no issues, which is now asserted rather than assumed;
- removing the integration left its stored readings, synchronisation checkpoints, and
  installation secret in Home Assistant's storage directory. Those files hold the
  account number, the supply-point number, and every collected interval, and both the
  privacy statement and the user guide said they were deleted. They now are, including
  every ledger month, while another entry's data and unrelated Home Assistant storage
  are untouched. The installation secret is shared between entries, so it goes only
  with the last one;
- the latest-reported-interval sensor declared `state_class: measurement` with the
  energy device class, which Home Assistant rejects. It logged "state class
  'measurement' which is impossible considering device class ('energy')" on every setup
  and pointed the user at this repository's issue tracker. It now carries no state
  class, because a 30-minute total replaced by the next one is not a running sum;
  long-term history comes from the published external statistics;
- the agreements query asked for `product.rates`, which an account user may not read.
  The resulting authorisation error propagated to the nearest nullable parent, so the
  whole product came back null and the **current plan name was lost** — to fetch a field
  the integration never publishes. Removing it resolves the plan name and clears the
  error, moving the agreements capability from partial to available;
- Energy Dashboard statistics are skipped with one warning when the recorder is not
  enabled, instead of raising `KeyError: recorder_instance`. `after_dependencies` orders
  the recorder but does not require it;
- `devices` and `registers` were queried without the `first` argument the
  provider's GraphQL guide requires on every paginated field. A conformance test
  now scans every shipped query, so a connection added later cannot omit it;
- user documentation claimed OEJP serves "roughly the last 30 days" of history.
  Measured on a real account, every interval since supply started is retrievable;
  the 31-day figure is a per-response cap of 1488 intervals that silently keeps the
  newest and drops the oldest. The scoped contract had this right and the user
  documentation contradicted it;
- an unreachable long-backfill planner was removed from `sync.py`, together with the
  background reason and priority nothing produced, and the two design documents that
  described it as an available feature. Why a long backfill is deliberately absent is
  recorded in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md);
- `OejpDeviceAuthSession` was a subclass with a docstring and no body, implying a
  capability that did not exist. Device-grant tokens come from the same token endpoint
  and refresh identically, so `OejpPkceAuthSession` serves them unchanged;
- the device-authorization endpoint was recorded as absent, which left the
  implemented RFC 8628 client unconstructible. The provider documents
  `/device-authorization/` and the live endpoint answers a POST with
  `invalid_request: Invalid client_id parameter value`, so it exists and only the
  client ID is missing. Its absence from the discovery document is a metadata gap.

### Provider behaviour confirmed against a real account

- the market name must carry a territory prefix. `JPN_ELECTRICITY` is accepted and
  `ELECTRICITY` is rejected with `KT-CT-4723`, which had silently disabled the whole
  generic reading path;
- a rejected credential is reported as `VALIDATION` with `KT-CT-1138`, not as an
  authentication error;
- one response returns at most 1488 intervals and drops the oldest beyond that silently,
  so every planned request stays well inside it;
- reading `version` switches from `DAILY` to `MONTHLY` when a billing period closes,
  which is the correction the ledger absorbs;
- provider monetary values are whole yen; and
- the generic reading API exposes no cost field at all.

### Deliberately absent

- **the per-interval cost figure Octopus Energy Japan returns.** Cost is published, but it
  is computed from the tariff. The provider's own figure applies one tier boundary where the
  tariff has two, combines the fuel-cost adjustment and the renewable levy into a single
  number, and cannot express the daily standing charge, so it does not add up to a billed
  total;
- any `kWh × unit price` estimate. One unit price cannot express tiered pricing, a daily
  standing charge, a monthly adjustment, an annual levy, and tax;
- a *current power* entity, because the API publishes 30-minute totals and an average
  presented as an instantaneous value would be wrong; and
- a *next meter reading* entity. The API exposes two such dates and both were measured in
  the past, disagreeing with the reading day on the same supply point.
