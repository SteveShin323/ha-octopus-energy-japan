# Privacy

This integration is read-only and runs entirely inside your Home Assistant. There
is no developer-operated server, no analytics, and no external telemetry. Nothing
about you is sent anywhere except to Octopus Energy Japan, on your behalf, to read
your own data.

## What leaves your Home Assistant

Requests to `https://api.oejp-kraken.energy/v1/graphql/` and to
`https://auth.oejp-kraken.energy/`, carrying your OAuth access token and the
identifiers needed to read your own account. That is all.

## What is stored, and where

Everything below lives in your Home Assistant's own storage.

| Data | Purpose | Removed when the entry is deleted |
|---|---|---|
| OAuth access and refresh tokens | authenticating to OEJP | yes |
| Account numbers, supply-point numbers, meter and register identifiers | required to call the API and to join stored readings back to a supply point | yes |
| Half-hourly readings, their version and quality, provider cost | totals and Energy Dashboard statistics that survive corrections | yes |
| Synchronisation checkpoints | resuming background work after a restart | yes |
| An installation-local secret | deriving stable private identities | yes |
| Energy Dashboard statistics | long-term energy history | **no**, see below |
| Application credentials (client ID) | re-adding without retyping | **no**, see below |

Your OEJP password is never requested, received, or stored. Sign-in happens on the
OEJP website.

## What never appears in the user interface

Raw provider identifiers are used only in private storage and in API calls. They do
not appear in entity names, entity IDs, unique IDs, device names, device
identifiers, states, or attributes.

Devices and statistics are addressed by an HMAC derived from an
installation-local secret plus the provider identifier. The result is stable inside
one Home Assistant installation and cannot be correlated with the same OEJP
identifier in a different installation.

Addresses and names are never used for device or entity names. Whole bill,
transaction, payment, and reading collections are never placed in state attributes.

## Diagnostics

The diagnostics download is designed so you can attach it to a public issue without
reading it first. It contains no token, email address, name, address, account
number, supply-point number, meter identifier, reading value, provider cost,
balance, or bill amount, and it reports failures by exception class name rather than
message, because provider text is unbounded.

The full contract is in
[`docs/DIAGNOSTICS_AND_REPAIRS.md`](docs/DIAGNOSTICS_AND_REPAIRS.md).

## Logs

The integration does not log tokens, identifiers, addresses, reading values, or
provider message text. Home Assistant's own debug logging can still be verbose, so
please attach diagnostics rather than logs when reporting a problem.

## What survives removal, and why

**Energy Dashboard statistics** stay in the Home Assistant recorder. Deleting an
integration should not destroy your energy history. Remove them yourself under
**Developer tools → Statistics**.

**Application credentials** stay so you can re-add the integration without
retyping the client ID. Remove them under **Settings → Devices & services →
Application credentials**.

## Development data

No production credential is stored in this repository or in CI. Live API
investigation uses a local, allow-listed, read-only probe that replaces customer
values with synthetic placeholders before anything reaches disk, and a second
scanner rejects a fixture that still contains one. The process is documented in
[`docs/FIXTURE_REDACTION.md`](docs/FIXTURE_REDACTION.md).

## Reporting a privacy problem

Please follow [`SECURITY.md`](SECURITY.md). Do not open a public issue containing
your own account data.
