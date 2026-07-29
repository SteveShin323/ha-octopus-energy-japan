# Octopus Energy Japan for Home Assistant

Unofficial Home Assistant custom integration for Octopus Energy Japan (OEJP).

> [!WARNING]
> This project is not affiliated with or endorsed by Octopus Energy Japan.

## Status

Early development. The initial target is read-only access to OEJP account and half-hourly electricity consumption data through the official OEJP GraphQL API.

## Planned MVP

- UI-based setup with email and password
- Account and supply-point discovery
- Half-hourly electricity consumption
- Today, yesterday, and current-month energy sensors
- Home Assistant Energy Dashboard compatibility
- Reauthentication and diagnostics
- HACS-compatible repository layout

## API

- GraphQL endpoint: `https://api.oejp-kraken.energy/v1/graphql/`
- Official documentation: `https://docs.oejp-kraken.energy/graphql/guides/`
- Official example: `https://github.com/octoenergy/oejp-api-example`

## Installation

The integration is not ready for installation yet.

## Development

The Home Assistant integration domain is `octopus_energy_japan`.

```text
custom_components/octopus_energy_japan/
```

## License

MIT
