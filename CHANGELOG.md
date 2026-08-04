# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

No version has been released yet. The OAuth methods wait on a client ID from Octopus
Energy Japan; see
[`docs/OAUTH_APPLICATION_STATUS.md`](docs/OAUTH_APPLICATION_STATUS.md). The provider's
email and password login works today and is selectable at setup.

## [Unreleased]

### Added

- a choice of sign-in method at setup. **Email and password** works without an OAuth
  client ID, storing the credential because the provider's refresh token lasts seven
  days and renewing does not extend it. **Octopus Energy Japan account** is the
  recommended method and never sees the password. One OEJP login owns one config entry
  under either method, so an entry can be promoted to OAuth in place — keeping its
  readings and statistics, and deleting the stored password. Removing such an entry
  deletes the local credential but cannot revoke the token at OEJP, which does not
  permit an account user to invalidate a refresh token; it expires there within seven
  days. See [ADR 0008](docs/adr/0008-password-authentication.md);
- read-only OEJP integration: OAuth with PKCE, account and supply-point discovery,
  generic and legacy reading providers with an explicit fallback policy;
- a persistent correction-aware interval ledger and Asia/Tokyo calendar aggregation;
- consumption, timestamp, delay, and lifecycle entities per supply point and
  direction, plus data-availability binary sensors;
- correction-safe Energy Dashboard external statistics rebuilt from the ledger;
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
  through sign-in, with nothing naming this integration as the cause.

### Fixed

- `devices` and `registers` were queried without the `first` argument the
  provider's GraphQL guide requires on every paginated field. A conformance test
  now scans every shipped query, so a connection added later cannot omit it;
- user documentation claimed OEJP serves "roughly the last 30 days" of history.
  Measured on a real account, every interval since supply started is retrievable;
  the 31-day figure is a per-response cap of 1488 intervals that silently keeps the
  newest and drops the oldest. The scoped contract had this right and the user
  documentation contradicted it;
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
- `halfHourlyReadings` caps one response at roughly 1476 intervals and narrows the
  window silently, so every planned request stays well inside it;
- reading `version` switches from `DAILY` to `MONTHLY` when a billing period closes,
  which is the correction the ledger absorbs;
- provider monetary values are whole yen; and
- the generic reading API exposes no cost field at all.

### Deliberately absent

- electricity cost. OEJP's per-interval `costEstimate` follows a simplified rate
  model that does not reproduce the billed tariff, so presenting it as cost would
  carry provider authority for a figure no line of the bill supports;
- tariff unit prices, because rate attribution is expressed through an untyped
  provider payload; and
- any `kWh × unit price` estimate.
