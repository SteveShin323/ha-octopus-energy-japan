# Octopus Energy Japan for Home Assistant

[![Validate](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/validate.yml/badge.svg?branch=main&event=push)](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/validate.yml)
[![Coverage](https://codecov.io/gh/SteveShin323/ha-octopus-energy-japan/branch/main/graph/badge.svg)](https://codecov.io/gh/SteveShin323/ha-octopus-energy-japan)
[![Security](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/security.yml)
[![CodeQL](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/github/license/SteveShin323/ha-octopus-energy-japan)](LICENSE)
![Project status](https://img.shields.io/badge/status-pre--alpha-orange)

An unofficial, read-only Home Assistant custom integration for Octopus Energy
Japan (OEJP). It brings your half-hourly electricity readings, calendar totals, and
correction-safe Energy Dashboard statistics into Home Assistant.

日本語の案内は [`docs/ja/README.md`](docs/ja/README.md) にあります。

> [!WARNING]
> This project is not affiliated with, endorsed by, or supported by Octopus
> Energy Japan or Kraken Technologies.

> [!IMPORTANT]
> **Sign in with your email and password for now.** OEJP has not issued an OAuth
> client ID and offers no self-service way to create one, so the account sign-in
> cannot be completed yet. The provider's email and password login works today and is
> offered at setup. Progress on the client ID is tracked in
> [`docs/OAUTH_APPLICATION_STATUS.md`](docs/OAUTH_APPLICATION_STATUS.md).

## Start here

| If you want to | Read |
|---|---|
| use the integration | [User guide](docs/USER_GUIDE.md) · [日本語](docs/ja/README.md) |
| know what data leaves your house | [Privacy](PRIVACY.md) |
| report a problem | [Diagnostics](docs/DIAGNOSTICS_AND_REPAIRS.md) · [日本語](docs/ja/DIAGNOSTICS.md) |
| contribute code | [Contributing](CONTRIBUTING.md) · [Architecture](docs/MASTER_TECHNICAL_DESIGN_V3.md) |

## What it does

- half-hourly import and export readings for every electricity supply point on
  every account your OEJP login can see;
- today, yesterday, week, and month totals on Asia/Tokyo calendar boundaries, which
  report **unknown** rather than a number that is quietly too low;
- Energy Dashboard statistics rebuilt from a persistent interval ledger, so a
  reading OEJP corrects later rewrites history deterministically;
- optional account, contract, and billing summaries, with financial entities off by
  default; and
- diagnostics you can attach to a public issue without reading them first.

It never writes to your OEJP account, never asks for your OEJP password, and sends
nothing to any server the developer runs.

## What it deliberately does not do

**No electricity cost.** OEJP publishes a per-interval `costEstimate`. Measured
against a real invoice it follows a simplified rate model that does not reproduce
the billed tariff — one tier boundary where the tariff has two, and no way to
express the fixed daily standing charge. Presenting it as your cost would give
provider authority to a figure no line of your bill supports. The measurement is in
[`docs/CONTRACT_AND_BILLING.md`](docs/CONTRACT_AND_BILLING.md).

**No tariff unit prices, and no `kWh × unit price` estimate.** A Japanese bill
combines tiered pricing, a fixed daily charge, a monthly fuel-cost adjustment, a
renewable levy, and tax. One unit price cannot reproduce it.

**No gas.** Electricity only.

## Project status

| Area | Status |
|---|---|
| Sign-in methods | Email/password, OAuth authorization code, and device code — selectable at setup |
| OAuth architecture | Implemented; provider endpoints and scopes confirmed |
| Account and supply-point discovery | Implemented |
| Import/export reading providers | Implemented |
| Correction-aware ledger and JST aggregation | Implemented |
| Home Assistant runtime and entities | Implemented and validated |
| Energy Dashboard statistics | Implemented and validated |
| Account, contract, and billing summaries | Implemented and validated |
| Diagnostics and Repairs | Implemented and validated |
| User documentation and release process | Implemented |
| Provider-issued cost and tariff rates | Excluded, with evidence |
| Supported installation | Works with email/password; the account method needs an OEJP client ID |

The badges report `main`. Validate runs Ruff, strict mypy, pytest with branch
coverage, Hassfest, HACS validation, and documentation link checks.

## Installation

Add the repository to HACS, install **Octopus Energy Japan**, restart, then add the
integration and choose a sign-in method. Full steps, and the trade-offs between the
methods, are in the [user guide](docs/USER_GUIDE.md#installation).

## Documentation

**For users**

- [User guide](docs/USER_GUIDE.md) — entities, update behaviour, limitations,
  troubleshooting, removal
- [日本語ガイド](docs/ja/README.md), [Energy Dashboard](docs/ja/ENERGY_DASHBOARD.md),
  [契約・請求](docs/ja/CONTRACT_AND_BILLING.md), [診断情報](docs/ja/DIAGNOSTICS.md)
- [Privacy](PRIVACY.md)

**For contributors**

- [Master technical design v3](docs/MASTER_TECHNICAL_DESIGN_V3.md) — normative
  architecture and phase plan
- [API contracts](docs/API_CONTRACTS.md) — every provider behaviour, with the date
  it was observed
- [Ledger and aggregation](docs/LEDGER_AND_AGGREGATION.md),
  [Runtime and entities](docs/RUNTIME_AND_ENTITIES.md),
  [Energy statistics](docs/ENERGY_STATISTICS.md),
  [Contract and billing](docs/CONTRACT_AND_BILLING.md),
  [Diagnostics and repairs](docs/DIAGNOSTICS_AND_REPAIRS.md)
- [OAuth application status](docs/OAUTH_APPLICATION_STATUS.md) — the release blocker
- [Fixture redaction](docs/FIXTURE_REDACTION.md) — how live API investigation works
- [Release process](docs/RELEASE_PROCESS.md), [Changelog](CHANGELOG.md)
- [Architecture decisions](docs/adr/)

Earlier designs are kept under [`docs/archive/`](docs/archive/) as research history.
Design v3 takes precedence. English is the normative repository language; Japanese
user documentation is maintained alongside it.

## API

- GraphQL endpoint: `https://api.oejp-kraken.energy/v1/graphql/`
- Auth server: `https://auth.oejp-kraken.energy`
- Official documentation: `https://docs.oejp-kraken.energy/graphql/guides/`
- Official example: `https://github.com/octoenergy/oejp-api-example`

The design also incorporates code-level review of `mapplebox/oejp`,
`Shuangbing/oejp-hacs`, `lvctr/hass-oejp`, and
`strongbugman/ha-octopusenergy-oejp`.

## Contributing

The architecture is separated into authentication, transport, operations and
parsers, discovery, providers, ledger, aggregation, statistics, coordinators, and
entities. Contributions need tests at the relevant boundary and must preserve
delayed and corrected-reading semantics.

One rule matters more than the rest: **do not assert provider behaviour that was
not observed or published.** A shape taken from documentation alone is labelled
unverified until a probe confirms it. Several defects in this project's history came
from skipping that.

Read [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), and
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) before opening a pull request.

Live API investigation must use the allow-listed local probe and synthetic-fixture
process in [`docs/FIXTURE_REDACTION.md`](docs/FIXTURE_REDACTION.md). Raw responses
and credentials must never be committed.

## License

MIT
