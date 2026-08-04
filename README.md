<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/octopus_energy_japan/brand/dark_logo.png">
  <img src="custom_components/octopus_energy_japan/brand/logo.png" alt="Octopus Energy Japan" height="72">
</picture>

# Octopus Energy Japan for Home Assistant

[![Validate](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/validate.yml/badge.svg?branch=main&event=push)](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/validate.yml)
[![Coverage](https://codecov.io/gh/SteveShin323/ha-octopus-energy-japan/branch/main/graph/badge.svg)](https://codecov.io/gh/SteveShin323/ha-octopus-energy-japan)
[![Security](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/security.yml)
[![CodeQL](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/SteveShin323/ha-octopus-energy-japan/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/github/license/SteveShin323/ha-octopus-energy-japan)](LICENSE)
![Project status](https://img.shields.io/badge/status-pre--alpha-orange)

A read-only Home Assistant custom integration for Octopus Energy Japan. It brings
your half-hourly electricity readings, calendar totals, and correction-safe Energy
Dashboard statistics — including cost — into Home Assistant.

日本語の案内は [`docs/ja/README.md`](docs/ja/README.md) にあります。

> [!WARNING]
> Unofficial. Not affiliated with, endorsed by, or supported by Octopus Energy Japan
> or Kraken Technologies.

## What it does

- Half-hourly import and export readings for every electricity supply point on every
  account your login can see.
- Today, yesterday, week, and month totals on Asia/Tokyo calendar boundaries.
- Energy Dashboard statistics rebuilt from a persistent interval ledger, so a reading
  revised later rewrites history deterministically.
- An hourly cost statistic computed from your own tariff — price steps, standing
  charge, fuel-cost adjustment, and renewable levy. You enter no prices.
- Optional account, contract, and billing summaries, with financial entities off by
  default.
- Diagnostics you can attach to a public issue without reading them first.

It is read-only: it never changes your Octopus Energy Japan account, submits no meter
readings, and makes no payments. Nothing is sent to any server the developer runs.

## What it supports

There is no hardware to pair. The integration talks to the API, so it works with
whatever your account exposes.

| Supported | Detail |
|---|---|
| Electricity supply points | every point on every account your login can see |
| Multiple accounts | one Home Assistant entry covers all accounts for one login |
| Import and export | export appears only when reported for your supply point |
| Meters, devices, registers | discovered when exposed; not required |
| Ended accounts and supply points | discovered, disabled by default, selectable |
| Gas | not supported. Electricity only |

## Typical uses

- Watch yesterday's and this month's electricity use without opening the provider app.
- Put consumption and cost on the Energy Dashboard beside solar, battery, or a
  locally measured circuit.
- Automate on consumption, such as a notification when today's use passes a threshold.
- Keep long-term history. The integration stores every interval it collects, so your
  history keeps growing.

It is not a billing tool. See [known limitations](#known-limitations).

## Installation

1. Add this repository to HACS and install **Octopus Energy Japan**.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → Octopus Energy Japan**.
4. Choose a sign-in method.

### Choosing a sign-in method

| | Email and password | Device code | Account (browser) |
|---|---|---|---|
| Usable today | **yes** | needs a client ID | needs a client ID |
| Where you sign in | in Home Assistant | on the provider website, from any device | on the provider website |
| Your password | **stored in Home Assistant** | never requested | never requested |
| Needs My Home Assistant | no | no | yes |

**Email and password** works today. Both are stored in Home Assistant — not for
convenience, but because the refresh token lasts seven days and renewing it does not
extend that, so nothing but the credential itself can sign in afterwards. What is
stored and where is in [`PRIVACY.md`](PRIVACY.md). This is the provider's older login
and can stop being accepted without notice; Home Assistant then asks you to reconnect.

**Device code** and **Account** are the two OAuth methods. Both are implemented and
both need a public OAuth client ID registered under **Settings → Devices & services →
Application credentials**. No client ID is published for this provider, so neither can
be completed yet, and choosing one stops with a message asking for a credential first.
Device code needs no browser redirect, which makes it the better of the two for an
instance with no public address.

### Switching later, without losing history

Open the integration's menu, choose to reconnect, and pick another method. The entry
is promoted in place: readings and Energy Dashboard statistics are kept, and any
stored password is deleted. Do not delete and re-add.

### My Home Assistant

Required for the account method only. It is part of `default_config:`, so a normal
installation already has it. Sign-in returns through `my.home-assistant.io`, the one
registered redirect address, which forwards the result to your instance. On a
stripped-down configuration, add `my:` to `configuration.yaml` and restart.

### Configuration parameters

No API key, no account number, no supply point. Everything else is discovered from the
account you sign in with.

| Parameter | Where | Required for | Meaning |
|---|---|---|---|
| Email and password | setup → **Email and password** | that method | your provider sign-in, stored so the integration can sign in again |
| OAuth client ID | **Application credentials** | the two OAuth methods | a public client ID. Not a secret; leave the secret field empty |
| Enabled historical resources | integration → **Configure** | nothing | which ended accounts or supply points keep reporting |

Active accounts and supply points are always enabled, so a new supply point starts
reporting on its own.

## Entities

Every entity belongs to a device: one per account, and one per supply point beneath
it. Names are ordinal — `OEJP supply point 1-1` — and carry no account number, supply
point number, or address, so a screenshot or a pasted automation carries none either.

To tell which supply point a device is, open its device page: the supply point number
(供給地点特定番号) is shown as the serial number, and the account device shows your
account number. Those stay off entity IDs, states, attributes, and diagnostics.

### Per supply point

Enabled by default:

| Entity | What it is |
|---|---|
| Latest reported interval consumption | the newest 30-minute value published |
| Consumption today / yesterday | calendar totals in Asia/Tokyo |
| Consumption this week / this month / last month | calendar totals in Asia/Tokyo |
| Latest reading timestamp | when the newest interval ended |
| Data delay | how far behind the newest interval is |
| Status | whether the supply point is active or ended |
| Data available | whether readings are currently arriving |
| Meter reading day | the day of the month this meter is read on |

Disabled by default: **Address** — the address held for this supply point's property,
so that with more than one property you can tell which device is which. It is off
until you enable it because an entity state is written to the recorder database and
included in backups. See [`PRIVACY.md`](PRIVACY.md).

A calendar total reports **unknown** until the whole period is covered, rather than a
number that is quietly too low. A partly synchronised day is not a smaller day.

There is no *current power* entity: the API publishes 30-minute totals, not live
power, and presenting an average as instantaneous would be wrong. There is no *next
meter reading* entity either — the two dates the API exposes for it are not kept up to
date.

### Per account

Enabled by default: account status, current product, current agreement start and end.

Disabled by default, because they are financial: account balance, overdue balance,
latest bill amount, latest bill issued date, latest bill payment due date, latest
transaction amount.

## How data updates

| What | How often |
|---|---|
| Consumption | every 30 minutes, re-reading the last 72 hours |
| Account and supply-point discovery | every 24 hours |
| Contract and billing | every 12 hours |
| Full reconciliation | daily, over the current and previous month |

**Readings arrive late.** An interval is published several hours after it happens,
sometimes longer. Nothing is missing; it has not been published yet.

**Readings get corrected.** When a billing period closes, intervals are reissued with
a new version and values can change. The integration stores every interval rather than
a running total, so a correction rewrites the affected history and every later Energy
Dashboard total deterministically.

**Startup does not stall.** Setup completes from recent data; older history is fetched
in the background afterwards.

## Energy Dashboard

**Settings → Dashboards → Energy → Grid consumption → Add consumption**, then pick the
statistic named after your supply point, for example `OEJP supply point 1-1 Import
energy`. If an export direction is reported, add `… Export energy` under **Return to
grid**.

Pick the **statistic**, not a period sensor. The period sensors are for display; the
statistics are the correction-safe source Home Assistant can rewrite.

Set cost to `OEJP supply point 1-1 Import cost`. Leave the price fields empty — Home
Assistant only multiplies a *sensor* by a price and skips that for an external
statistic, so anything entered there is ignored. The cost statistic is the route.

There is no export cost statistic. Energy fed back is compensated under a different
arrangement than consumption, and pricing it at a consumption rate would invent a
payment. Leave **Return to grid** without a cost.

Treat cost as a good estimate, not your bill. Measured against one real closed bill it
came to 104% of the billed total, for two reasons described under
[known limitations](#known-limitations).

## Known limitations

**Cost is an estimate.** The tariff is read from your own agreement, so the prices are
yours rather than a guess. Two things limit the total: your bill runs to a meter read a
few hours after midnight while the price steps here restart on the Tokyo calendar
month, and only the current month's fuel-cost adjustment is available, so hours from
earlier months are priced without one.

**The provider's own per-interval cost figure is not published.** It collapses the
fuel-cost adjustment and the renewable levy into one number and cannot express the
daily standing charge, so it does not reproduce a billed total.

**A first install starts with the current and previous month.** Older history is
retrievable and is fetched in the background afterwards.

**Calendar totals are not billing periods.** Your bill runs to a meter-read time a few
hours after midnight; these months are Asia/Tokyo calendar months. The two will not
match, by design.

**Contract information may be unavailable.** Some accounts are not authorised for
agreement data. Consumption is unaffected; those entities stay unavailable and a
repair message explains it.

## Troubleshooting

Start with **Settings → System → Repairs**. The integration raises a plain-language
message for each condition it can detect, and each says whether you need to act.
Usually you do not.

| Symptom | Likely cause |
|---|---|
| Setup says My Home Assistant is required | `my` is not loaded. Add `my:` or restore `default_config:` and restart. Account method only |
| Asked to reconnect a password entry | your password changed, or password login stopped being accepted |
| Totals say **unknown** | the period is not fully covered yet. Normal soon after install |
| Latest interval is hours old | normal publishing delay. Check *Data delay* |
| Entities went unavailable together | a refresh failed. The integration retries automatically |
| Financial entities missing | they are disabled by default. Enable them individually |
| Energy Dashboard totals changed retroactively | an interval was corrected. Expected |

### Reporting a problem

Download diagnostics from the integration's menu and attach the file to a
[GitHub issue](https://github.com/SteveShin323/ha-octopus-energy-japan/issues). It
contains no token, email address, account number, supply point number, address,
reading value, or monetary amount, so you do not need to review it first. Please do
not paste Home Assistant logs, which can contain provider text.

## Removing the integration

**Settings → Devices & services → Octopus Energy Japan → Delete.** This deletes the
entry's stored readings, sync checkpoints, installation secret, and any stored
credential. An OAuth entry also revokes its authorization; a password entry cannot,
because an account user may not invalidate a refresh token, so that token expires
within seven days instead. To cut access off at once, change your password.

Two things survive on purpose:

- **Energy Dashboard statistics** stay in the recorder, so removing the integration
  does not destroy your energy history. Remove them under **Developer tools →
  Statistics**.
- **Application credentials** stay, so you can re-add without re-entering the client
  ID. Remove them under **Settings → Devices & services → Application credentials**.

Re-adding starts a fresh local history and downloads the current and previous month
again.

## Privacy

Your data stays in your Home Assistant. There is no telemetry and no
developer-operated server. See [`PRIVACY.md`](PRIVACY.md).

## Project status

Pre-alpha. Everything listed under [what it does](#what-it-does) is implemented and
covered by tests, verified against a real account. Outstanding:

| Item | State |
|---|---|
| The two OAuth sign-in methods | implemented; need a published client ID to complete |
| Icon and logo in `home-assistant/brands` | not submitted |

## Documentation

- [Privacy](PRIVACY.md) — what is stored, what leaves your instance, what survives removal
- [Architecture](docs/ARCHITECTURE.md) — how the integration is built
- [API contracts](docs/API_CONTRACTS.md) — provider behaviour a contributor must not break
- [Development](docs/DEVELOPMENT.md) — tests, live probes, releases
- [Decision records](docs/adr/) — why the design is what it is
- [Changelog](CHANGELOG.md)

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), and
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) first.

One rule matters more than the rest: **do not assert provider behaviour that was not
observed.** A shape taken from documentation alone is unverified until a probe confirms
it. Several defects in this project's history came from skipping that.

## License

MIT
