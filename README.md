# Octopus Energy Japan for Home Assistant

An unofficial, read-only Home Assistant custom integration for Octopus Energy
Japan (OEJP).

> [!WARNING]
> This project is not affiliated with, endorsed by, or supported by Octopus
> Energy Japan or Kraken Technologies.

## Status

Pre-alpha architecture and API development. The integration is **not ready to
install**.

OEJP has been asked to issue a public OAuth application for Authorization Code
with PKCE and, if available, Device Authorization Grant. The project will not
publish a functional release until the client and read-only permission model are
confirmed. The public integration will not request or store an OEJP password.

## Technical design

The current normative architecture and implementation plan is:

- [`docs/MASTER_TECHNICAL_DESIGN_V3.md`](docs/MASTER_TECHNICAL_DESIGN_V3.md)
- [`docs/OAUTH_APPLICATION_STATUS.md`](docs/OAUTH_APPLICATION_STATUS.md)
- [`docs/adr/`](docs/adr/)

It incorporates the official OEJP GraphQL documentation and code-level reviews
of:

- `mapplebox/oejp`
- `Shuangbing/oejp-hacs`
- `lvctr/hass-oejp`
- `strongbugman/ha-octopusenergy-oejp`

Earlier designs remain under [`docs/archive/`](docs/archive/) as research
history. Design v3 takes precedence.

## Planned capabilities

- OEJP-hosted OAuth with automatic token refresh and Home Assistant
  reauthentication
- Multiple accounts, supply points, meters/registers, and import/export
- Generic and legacy OEJP reading providers with explicit capability fallback
- Persistent correction-aware interval ledger
- Today, yesterday, week, month, and latest-interval entities
- Deterministic Home Assistant Energy Dashboard statistics
- Optional account, agreement, tariff, official-cost, and billing summaries
- Diagnostics with identifier and credential redaction
- English and Japanese user interfaces and documentation

## API

- GraphQL endpoint: `https://api.oejp-kraken.energy/v1/graphql/`
- Official documentation: `https://docs.oejp-kraken.energy/graphql/guides/`
- Official example: `https://github.com/octoenergy/oejp-api-example`

## Installation

There is no supported installation yet. The Home Assistant setup flow no longer
accepts an OEJP email address or password. OAuth setup remains intentionally
unavailable until OEJP confirms the production endpoints, scopes, authorization
header scheme, and public-client terms recorded in
[`docs/OAUTH_APPLICATION_STATUS.md`](docs/OAUTH_APPLICATION_STATUS.md).

## Development

The Home Assistant integration domain is `octopus_energy_japan`.

```text
custom_components/octopus_energy_japan/
```

Development requirements and commands are in
[`CONTRIBUTING.md`](CONTRIBUTING.md). English is the normative repository
language. Japanese user documentation and UI translations will be maintained
for public releases; no other repository translation is planned.

## Privacy

OAuth tokens and raw provider identifiers remain inside the user's Home
Assistant installation. The integration will not implement external telemetry.
Email, tokens, account numbers, supply-point identifiers, addresses, names, and
raw billing/reading data must not appear in public diagnostics or logs.

## Contributing

The architecture is intentionally separated into authentication, transport,
operations/parsers, discovery, providers, ledger, aggregation, statistics,
coordinators, and entities. Contributions must include tests at the relevant
boundary and preserve delayed/corrected-reading semantics.

Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`SECURITY.md`](SECURITY.md) before opening a pull request or security report.

## License

MIT
