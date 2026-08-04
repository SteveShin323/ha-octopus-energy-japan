# Privacy

This integration is read-only and runs entirely inside your Home Assistant. There is
no developer-operated server, no analytics, and no telemetry. Nothing about you is
sent anywhere except to Octopus Energy Japan, on your behalf, to read your own data.

## What differs by sign-in method

The sign-in method you choose is the one thing that changes what is stored about you.

| | Email and password | Device code or account (OAuth) |
|---|---|---|
| Where you sign in | in Home Assistant | on the provider website |
| Your password | **stored in Home Assistant** | never requested, received, or stored |
| Stored to stay signed in | your email, your password, and the provider's tokens | access and refresh tokens |

The password is stored because the refresh token lasts seven days and renewing it does
not extend that, so nothing else can sign in again afterwards. It is kept in the config
entry, a plain-text file inside your Home Assistant configuration directory. It is
never logged, never shown in a state or attribute, and never included in diagnostics.

Switching that entry to an OAuth method **deletes** the stored password and keeps your
collected history. The reasoning is in
[ADR 0008](docs/adr/0008-password-authentication.md).

## What leaves your Home Assistant

Requests to `https://api.oejp-kraken.energy/v1/graphql/` and
`https://auth.oejp-kraken.energy/`, carrying your access token and the identifiers
needed to read your own account. That is all.

## What is stored, and where

Everything below lives in your Home Assistant's own storage.

| Data | Purpose | Deleted with the entry |
|---|---|---|
| Access and refresh tokens | authenticating | yes |
| Your email and password, with that sign-in method | signing in again after seven days | yes |
| Account, supply point, meter, and register identifiers | calling the API and joining stored readings to a supply point | yes |
| Half-hourly readings with version and quality | totals and statistics that survive corrections | yes |
| Your tariff prices | computing the cost statistic | yes |
| Property address and postcode | the optional Address entity | yes |
| Synchronisation checkpoints | resuming background work after a restart | yes |
| An installation-local secret | deriving stable private identities | with the last entry |
| Energy Dashboard statistics | long-term energy history | **no**, see below |
| Application credentials (client ID) | re-adding without retyping | **no**, see below |

## What never appears in the user interface

Raw provider identifiers do not appear in entity names, entity IDs, unique IDs, device
names, device identifiers, states, or attributes. Those are the places a value travels
without you choosing to show it: a screenshot, an automation pasted into a forum, a
state history export.

Devices and statistics are addressed by an HMAC of an installation-local secret and the
provider identifier. It is stable inside one installation and cannot be correlated with
the same identifier in another.

**Two deliberate exceptions.**

Each device page shows a serial number — your account number on an account device, the
supply point number (供給地点特定番号) on a supply point. Without it you could not tell
which of two supply points a device is. It is a single field on a page you open
yourself, never part of an entity ID or a state, and not in diagnostics.

The property **address** is available as an entity that is **disabled by default**,
because with more than one property nothing else tells you which device is which.
Enabling it is a real choice: an entity state is written to the recorder database,
included in backups, and readable by voice assistants and anyone with dashboard access.
The address is never used for a device or entity name and is not in diagnostics; a test
asserts the latter.

Whole bill, transaction, payment, and reading collections are never placed in state
attributes.

## Diagnostics

The diagnostics download is designed so you can attach it to a public issue without
reading it first. It contains no token, email address, name, address, account number,
supply point number, meter identifier, reading value, balance, or bill amount, and it
reports failures by exception class name rather than message, because provider text is
unbounded. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Logs

The integration does not log tokens, identifiers, addresses, reading values, or
provider message text. Home Assistant's own debug logging can still be verbose, so
please attach diagnostics rather than logs when reporting a problem.

## What survives removal, and why

Deleting the entry deletes every row marked yes above, rather than orphaning it.

**Energy Dashboard statistics** stay in the recorder, because deleting an integration
should not destroy your energy history. Remove them under **Developer tools →
Statistics**. **Application credentials** stay so you can re-add without retyping the
client ID; remove them under **Settings → Devices & services → Application
credentials**.

If you removed an entry from a build before deletion was implemented, files named
`octopus_energy_japan.ledger.*` and `octopus_energy_japan.sync.*` may remain in your
storage directory and can be deleted by hand.

**Removing an email and password entry cannot revoke its token.** An account user may
not invalidate a refresh token, so it expires seven days after the sign-in that issued
it. Removal deletes Home Assistant's copy of your email, password, and tokens. To cut
access off immediately, change your password. An OAuth entry *is* revoked, through the
provider's revocation endpoint.

## Development data

No production credential is stored in this repository or in CI. Live API investigation
uses a local, allow-listed, read-only probe that replaces customer values with
synthetic placeholders before anything reaches disk, and a scanner rejects a fixture
that still contains one. See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Reporting a privacy problem

Follow [`SECURITY.md`](SECURITY.md). Do not open a public issue containing your own
account data.
