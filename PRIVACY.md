# Privacy

This integration is read-only and runs entirely inside your Home Assistant. There
is no developer-operated server, no analytics, and no external telemetry. Nothing
about you is sent anywhere except to Octopus Energy Japan, on your behalf, to read
your own data.

## What differs by sign-in method

You choose a sign-in method when you add the integration, and it is the one thing
that changes what is stored about you.

| | Account or device code (OAuth) | Email and password |
|---|---|---|
| Where you sign in | on the OEJP website | in Home Assistant |
| Your password | never requested, received, or stored | **stored in Home Assistant** |
| What is stored to stay signed in | OAuth access and refresh tokens | your email, your password, and the provider's tokens |

The password is stored because the provider's refresh token lasts seven days and
renewing it does not extend that, so nothing else can sign in again afterwards. It is
kept in the config entry, which is a plain-text file inside your Home Assistant
configuration directory. It is never logged, never shown in a state or attribute, and
never included in diagnostics.

If you later switch that entry to the account sign-in, the stored password is
**deleted**, and your collected history is kept. The reasoning is recorded in
[ADR 0008](docs/adr/0008-password-authentication.md).

## What leaves your Home Assistant

Requests to `https://api.oejp-kraken.energy/v1/graphql/` and to
`https://auth.oejp-kraken.energy/`, carrying your OAuth access token and the
identifiers needed to read your own account. That is all.

## What is stored, and where

Everything below lives in your Home Assistant's own storage.

| Data | Purpose | Removed when the entry is deleted |
|---|---|---|
| OAuth access and refresh tokens | authenticating to OEJP | yes |
| Your email and password, if you chose that sign-in method | signing in again once the provider's seven-day refresh token expires | yes |
| Account numbers, supply-point numbers, meter and register identifiers | required to call the API and to join stored readings back to a supply point | yes |
| Half-hourly readings, their version and quality, provider cost | totals and Energy Dashboard statistics that survive corrections | yes |
| Synchronisation checkpoints | resuming background work after a restart | yes |
| An installation-local secret | deriving stable private identities | when the last entry is removed |
| Energy Dashboard statistics | long-term energy history | **no**, see below |
| Application credentials (client ID) | re-adding without retyping | **no**, see below |

With the account sign-in, your OEJP password is never requested, received, or
stored, and sign-in happens on the OEJP website. With the email and password method
it is stored, as described above.

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

The rows marked yes are deleted by removing the integration, not merely orphaned.
Before this was implemented the stored readings, synchronisation checkpoints, and
installation secret survived removal in Home Assistant's storage directory; if you
removed an entry from a build before that fix, files named
`octopus_energy_japan.ledger.*` and `octopus_energy_japan.sync.*` may still be present
and can be deleted by hand.

**Application credentials** stay so you can re-add the integration without
retyping the client ID. Remove them under **Settings → Devices & services →
Application credentials**.

**Removing an email and password entry cannot revoke its token at the provider.**
OEJP does not permit an account user to invalidate a refresh token: the mutation
exists, and calling it as the signed-in user is rejected as unauthorised. Removal
deletes Home Assistant's copy of your email, password, and tokens, and the refresh
token then expires on OEJP's side within seven days of the sign-in that issued it.
An OAuth entry *is* revoked, at the provider's OAuth revocation endpoint, which is a
separate mechanism.

If you want the credential to stop working immediately, change your OEJP password.

## Development data

No production credential is stored in this repository or in CI. Live API
investigation uses a local, allow-listed, read-only probe that replaces customer
values with synthetic placeholders before anything reaches disk, and a second
scanner rejects a fixture that still contains one. The process is documented in
[`docs/FIXTURE_REDACTION.md`](docs/FIXTURE_REDACTION.md).

## Reporting a privacy problem

Please follow [`SECURITY.md`](SECURITY.md). Do not open a public issue containing
your own account data.
