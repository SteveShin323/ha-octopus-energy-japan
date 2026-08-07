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
![Project status](https://img.shields.io/badge/status-stable-brightgreen)

See your Octopus Energy Japan electricity(TGオクトパスエナジー) use in Home Assistant — half-hourly readings,
daily and monthly totals, and Energy Dashboard statistics with cost.

The integration only reads. It never changes your account.

日本語の案内は [`docs/ja/README.md`](docs/ja/README.md) にあります。

> [!WARNING]
> Unofficial. Not affiliated with, endorsed by, or supported by Octopus Energy Japan
> or Kraken Technologies.

## What it does

- **Half-hourly readings** for every electricity supply point your login can see, import
  and export.
- **Daily, weekly, and monthly totals**, on Japan's calendar.
- **Energy Dashboard statistics.** Every reading is stored locally, so when the provider
  corrects one later, every total that used it is corrected too.
- **Cost, calculated from your own tariff** — price steps, standing charge, fuel-cost
  adjustment, and renewable levy. You never type in a price.
- **Account, contract, and billing summaries.** Optional, and the financial ones are off
  until you turn them on.
- **A diagnostics file that is safe to share.** It holds no personal data, so you can
  attach it to a public bug report without reading it first.

Nothing is sent anywhere except to Octopus Energy Japan. There is no server run by the
developer, and no telemetry.

## What it looks like

> [!NOTE]
> Every number below is made up. These come from a throwaway Home Assistant filled with
> synthetic data — no real usage, cost, or account details.

![The Home Assistant Energy Dashboard showing a day of hourly electricity use and its cost in yen](docs/images/energy-dashboard.png)

Each account is a device, holding the contract and billing summaries. The financial
entities are switched on here so you can see them.

![The device page for an account, listing balance, bill, and agreement sensors](docs/images/account-device.png)

## What it supports

There is no hardware to pair. It reads the Octopus Energy Japan API, so it supports
whatever your account exposes.

| | |
|---|---|
| Electricity supply points | every point on every account your login can see |
| Multiple accounts | one entry covers all accounts for one login |
| Import and export | export appears only if your supply point reports it |
| Meters, devices, registers | picked up when available; not required |
| Ended accounts and supply points | found, switched off by default, can be switched on |
| Gas | not supported — electricity only |

## Typical uses

- Check yesterday's or this month's electricity without opening the Octopus app.
- Show consumption and cost on the Energy Dashboard, next to solar or a battery.
- Trigger an automation when today's use passes a threshold.
- Build up long-term history. Every interval it collects is kept.

It is not a billing tool — see [known limitations](#known-limitations).

## Installation

1. Add this repository to HACS and install **Octopus Energy Japan**.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Octopus Energy Japan**.
4. Sign in with your Octopus Energy Japan email address and password.

That is the whole setup. There is no API key, account number, or supply point number to
find — everything else comes from the account you sign in with.

### About signing in

Your email address and password are **stored in Home Assistant**. That is not for
convenience. The provider's session lasts seven days and renewing it does not extend that,
so after a week nothing but the password itself can sign in again.
[`PRIVACY.md`](PRIVACY.md) lists exactly what is kept and where.

This is Octopus Energy Japan's older login, and it could stop working without warning. If
it does, Home Assistant will ask you to sign in again.

<details>
<summary>Why there is no "sign in with your browser" option</summary>

The integration also implements the two OAuth methods — **Account (browser)** and
**Device code**. Both work, and both were tested against the provider's own login server.
Neither is offered. Both need a client that Octopus Energy Japan issues, and the company
will not issue one. Asked directly on 2026-08-06, it replied that OAuth is not supported in
Japan and that it provides no API to individual customers.

The code is kept rather than deleted. Both methods return to the menu on their own if a
client ever appears — either one issued to this integration, or one of your own added under
**Settings → Devices & services → Application credentials**.

</details>

### Switching sign-in method later

Open the integration's menu and choose to reconnect. Your readings and statistics are kept
and any stored password is deleted. Do not delete and re-add the integration.

### Configuration parameters

| Setting | Where | Meaning |
|---|---|---|
| Email and password | during setup | your Octopus Energy Japan sign-in |
| Ended accounts and supply points | integration → **Configure** | which closed accounts or supply points keep reporting |

Active accounts and supply points are always on, so a new supply point starts reporting by
itself.

## Entities

Every entity belongs to a device — one device per account, and one per supply point under
it. Devices are named by position, like `OEJP supply point 1-1`. No account number, supply
point number, or address appears in a name, so you can share a screenshot or an automation
without exposing them.

To identify a device, open its page: the supply point number (供給地点特定番号) is shown
as the serial number, and an account device shows the account number. Neither appears in
entity IDs, states, attributes, or diagnostics.

### Per supply point

On by default:

| Entity | What it is |
|---|---|
| Latest reported interval consumption | the newest 30-minute value published |
| Consumption today / yesterday | totals on Japan's calendar |
| Consumption this week / this month / last month | totals on Japan's calendar |
| Latest reading timestamp | when the newest interval ended |
| Data delay | how far behind the newest interval is |
| Status | whether the supply point is active or ended |
| Data available | whether readings are arriving |
| Meter reading day | the day of the month your account reports for the meter read |

Off by default: **Address**, the address held for this supply point. Useful if you have
more than one property. It is off because an entity state is written to Home Assistant's
database and included in backups — see [`PRIVACY.md`](PRIVACY.md).

Two things you may expect but will not find:

- **No current power.** The API publishes 30-minute totals, not live power. Showing an
  average as if it were instantaneous would be misleading.
- **No next meter reading.** The two dates the API offers for it are not kept up to date.

A total shows **unknown** until every interval in that period has arrived. Otherwise a
half-loaded day would look like a real day with very little use.

### Per account

On by default: account status, current product, current agreement start and end.

Off by default, because they are financial: account balance, overdue balance, latest bill
amount, latest bill issued date, latest bill payment due date, latest transaction amount.

## How data updates

| What | How often |
|---|---|
| Readings | every 30 minutes, re-reading the last 72 hours |
| Account and supply point discovery | every 24 hours |
| Contract and billing | every 12 hours |
| Full reconciliation | daily, over this month and last |

**Readings arrive late** — usually about five hours behind the meter, sometimes longer.
That is the provider's own delay, not a fault. Nothing is missing; it has not been
published yet.

**Readings get corrected.** When a billing period closes, the provider reissues intervals
and values can change. Because every interval is stored rather than a running total, a
correction updates every Energy Dashboard total that used it.

**Setup is quick.** It finishes using recent data, then fetches the rest in the background.

## Energy Dashboard

1. Go to **Settings → Dashboards → Energy → Grid consumption → Add consumption**.
2. Pick the statistic named after your supply point, for example
   `OEJP supply point 1-1 Import energy`.
3. Set cost to `OEJP supply point 1-1 Import cost`.

Two things to watch:

- **Pick the statistic, not the sensor.** The consumption sensors listed above are for
  display. Only a statistic can be rewritten when a reading is corrected.
- **Leave the price fields empty.** Home Assistant only multiplies a *sensor* by a price;
  for a statistic it ignores whatever you type. The cost statistic replaces it.

If your supply point reports export, add `… Export energy` under **Return to grid**. Leave
it without a cost: energy you send back is paid for under a separate arrangement, so
pricing it as consumption would show money you are not charged.

Treat the cost as a close estimate rather than your bill. Checked against one closed bill
on one account, it came to 104% of the billed total — one measurement, on one plan, in one
service area. [Known limitations](#known-limitations) explains what makes it differ.

## Known limitations

### Cost is an estimate

Every price comes from your own agreement, so nothing is guessed. Three things still make
the total differ from your bill:

- Price steps restart on the day your meter is read. The provider publishes no time of day
  for the read, so each boundary can be a few hours out.
- Octopus Energy Japan only publishes the current month's fuel-cost adjustment. The
  integration keeps every one it sees, so accuracy improves the longer it runs. Until an
  hour's own adjustment has been collected, the nearest one is used.
- Whether the standing charge already accounts for your contracted amperage is not
  confirmed. Your diagnostics file records what the provider calls it.

### Some plans cannot be priced at all

A plan whose price changes by time of day, or that charges for something other than
kilowatt-hours, cannot be expressed by this calculation. When that happens, no cost
statistic is published and a repair message explains why. Your consumption, totals, and
energy statistics are unaffected. A partial price would look correct and be wrong.

The per-interval cost figure the provider returns is not shown either. It merges the
fuel-cost adjustment and the renewable levy into one number, and leaves out the daily
standing charge. It does not add up to a bill either.

### A first install starts with this month and last

Everything older is collected only when you press **Import full history** on a supply
point's device page. It takes hours, uses about a third of your account's hourly request
allowance, and stops by itself when it reaches the start of your readings.

What it collects goes into the Energy Dashboard statistics. It does **not** change the
today, this week, this month, or last month sensors, which only ever cover this month and
last.

Some accounts are served by the provider's older reading path. That path returns only the
most recent 31 days, however far back you ask. Rather than record one month as your whole
history, the collection stops and a repair message says why.

### Totals follow Japan's calendar, not your bill

Every total uses Asia/Tokyo days, weeks, and months, whatever timezone Home Assistant is
set to. Japan has one timezone and no daylight saving, and your electricity is measured and
billed in it, so a total on another timezone's boundary would match nothing.

Your bill covers the period between two meter reads, so these totals will not match it.
That is intended. The cost statistic does follow the meter-reading period.

### Contract information may be unavailable

Some accounts are not authorised for agreement data. Consumption is unaffected; those
entities stay unavailable and a repair message explains it.

## Troubleshooting

Start with **Settings → System → Repairs**. The integration raises a plain-language message
for each problem it can detect, and each one says whether you need to do anything. Usually
you do not.

| Symptom | Likely cause |
|---|---|
| Totals say **unknown** | the period is not fully covered yet. Normal soon after install |
| Latest interval is hours old | normal publishing delay. Check *Data delay* |
| Entities all went unavailable | a refresh failed. It retries by itself |
| Financial entities missing | they are off by default. Switch them on individually |
| Energy Dashboard totals changed retroactively | an interval was corrected. Expected |
| Asked to sign in again | your password changed, or the login stopped being accepted |

### Reporting a problem

Download diagnostics from the integration's menu and attach the file to a
[GitHub issue](https://github.com/SteveShin323/ha-octopus-energy-japan/issues). It contains
no token, email address, account number, supply point number, address, reading, or amount,
so you do not need to check it first.

Please do not paste Home Assistant logs — those can contain text from Octopus Energy Japan.

## Removing the integration

**Settings → Devices & services → Octopus Energy Japan → Delete.**

This deletes the stored readings, sync checkpoints, Energy Dashboard statistics,
installation secret, and your stored password. The password login cannot be revoked from
here, because Octopus Energy Japan gives customers no way to invalidate a session; it
expires within seven days. To cut access off at once, change your password.

One thing stays on purpose: an **application credential** you added by hand, so you do not
have to enter it again. Remove it under **Settings → Devices & services → Application
credentials**.

Re-adding starts a fresh local history and downloads this month and last again.

The old statistics cannot be continued, which is why they are deleted. Statistic names are
derived from the installation secret, and that secret is deleted with the entry, so a
re-added entry writes to new names. Keeping the old rows left two identically named series
in the Energy Dashboard picker.

If you removed an entry from an older build, clear the leftovers under
**Developer tools → Statistics**.

## Privacy

Your data stays in your Home Assistant. No telemetry, no developer-run server. See
[`PRIVACY.md`](PRIVACY.md).

## Project status

Stable as of 1.0.0. Everything under [what it does](#what-it-does) is implemented, tested,
and verified against a real account. Entity IDs, statistic IDs, and stored formats are
settled — changing one would break somebody's automation or dashboard, so it now needs a
major version.

Two things are worth knowing:

| | |
|---|---|
| The two OAuth sign-in methods | implemented and kept, but not offered — the provider will issue no client. [ADR 0001](docs/adr/0001-oauth-public-client.md) |
| Account shapes other than one account with one supply point | handled in code, but not yet verified against a real account of that shape. [API contracts](docs/API_CONTRACTS.md) |

## Documentation

- [Privacy](PRIVACY.md) — what is stored, what leaves your instance, what survives removal
- [Architecture](docs/ARCHITECTURE.md) — how the integration is built
- [API contracts](docs/API_CONTRACTS.md) — API behaviour a contributor must not break
- [Development](docs/DEVELOPMENT.md) — tests, live probes, releases
- [Decision records](docs/adr/) — why the design is what it is
- [Changelog](CHANGELOG.md)

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), and
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) first.

One rule matters more than the rest: **do not assert API behaviour you have not observed.**
Something read in documentation is unverified until a probe confirms it. Several defects in
this project came from skipping that.

## License

MIT
