# Octopus Energy Japan for Home Assistant

Unofficial Home Assistant custom integration for Octopus Energy Japan (OEJP).

> [!WARNING]
> This project is not affiliated with or endorsed by Octopus Energy Japan.

## Status

Early development. The initial target is read-only access to OEJP account and half-hourly electricity consumption data through the official OEJP GraphQL API.

## Technical design

The current normative architecture and implementation plan is:

- [`docs/MASTER_TECHNICAL_DESIGN_V2.md`](docs/MASTER_TECHNICAL_DESIGN_V2.md)

It incorporates the official OEJP GraphQL documentation and code-level reviews of:

- `mapplebox/oejp`
- `Shuangbing/oejp-hacs`
- `lvctr/hass-oejp`
- `strongbugman/ha-octopusenergy-oejp`

The earlier architecture documents remain as research history. Where they conflict with the master v2 design, the master v2 design takes precedence.

## Planned MVP

- UI-based setup with email and password
- Multiple-account and supply-point discovery
- Cached access-token lifecycle and reauthentication
- Half-hourly electricity consumption
- Persistent correction-aware interval ledger
- Today, yesterday, week, and current-month energy sensors
- Home Assistant Energy Dashboard compatibility through deterministic statistics
- Diagnostics with identifier and credential redaction
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