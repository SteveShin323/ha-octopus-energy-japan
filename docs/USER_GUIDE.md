# User guide

This is the reference for people running the integration. It is the normative
English documentation; the Japanese version is [`ja/README.md`](ja/README.md).

> [!IMPORTANT]
> The integration cannot be connected yet. Octopus Energy Japan has not issued an
> OAuth client ID, and there is no self-service way to create one, so the sign-in
> step has nothing to authenticate against. Everything else is finished and tested.
> Progress is tracked in [`OAUTH_APPLICATION_STATUS.md`](OAUTH_APPLICATION_STATUS.md).

## What it does

It reads your Octopus Energy Japan electricity data and puts it into Home
Assistant. Read-only: it never changes anything on your OEJP account, submits no
meter readings, and makes no payments.

You get half-hourly consumption, calendar totals, correction-safe Energy Dashboard
statistics, and optional account, contract, and billing summaries.

## What it supports

There is no hardware to pair. The integration talks to the OEJP API, so it works
with whatever OEJP exposes for your account.

| Supported | Detail |
|---|---|
| Electricity supply points | every point on every account your login can see |
| Multiple accounts | one Home Assistant entry covers all accounts for one login |
| Import and export | export appears only if OEJP reports it for your point |
| Meters, devices, registers | discovered when OEJP exposes them; not required |
| Ended accounts and supply points | discovered, disabled by default, selectable |
| Gas | **not supported.** OEJP's gas market is out of scope |

## Typical uses

- watch yesterday's and this month's electricity use without opening the OEJP app;
- put OEJP consumption on the Home Assistant Energy Dashboard next to solar,
  battery, or a home-assistant-measured circuit;
- automate on consumption, for example a notification when today's use passes a
  threshold;
- keep long-term history. OEJP serves roughly the last 30 days; the integration
  stores what it has already collected, so your history keeps growing past that.

It is **not** a billing tool. See [known limitations](#known-limitations).

## Installation

Once a client ID exists, installation is: add the repository to HACS, install
**Octopus Energy Japan**, restart Home Assistant, then **Settings → Devices &
services → Add integration → Octopus Energy Japan**.

Sign-in happens on the OEJP website, in your browser. The integration never asks
for your OEJP password and cannot see it.

**My Home Assistant must be enabled.** It is part of `default_config:`, so a normal
installation already has it. Sign-in returns through `my.home-assistant.io`, which
is the one redirect address registered with OEJP, and which forwards the result to
your own instance. If you run a stripped-down configuration, add `my:` to
`configuration.yaml`. The integration stops with an explanatory message rather than
letting OEJP reject the sign-in for a redirect address it does not recognise.

If you try to add the integration before registering an application credential,
Home Assistant stops with a message telling you to add one first. That is the
expected behaviour today, because no client ID exists to register.

### Configuration parameters

There is nothing to type during setup. No API key, no account number, no supply
point. Everything is discovered from the account you sign in with.

| Parameter | Where | Required | Meaning |
|---|---|---|---|
| OAuth client ID | Settings → Devices & services → **Application credentials** | yes | the public client ID issued by OEJP. Not a secret. Leave the secret field empty |
| Enabled historical resources | integration → **Configure** | no | which ended accounts or supply points to keep reporting |

Active accounts and supply points are always enabled and cannot be turned off, so a
new supply point starts reporting on its own.

## Entities

Every entity belongs to a device: one per account, and one per supply point beneath
it. Names contain no account number, supply-point number, or address.

### Per supply point and direction

Enabled by default:

| Entity | What it is |
|---|---|
| Latest reported interval consumption | the newest 30-minute value OEJP has published |
| Consumption today / yesterday | calendar totals in Asia/Tokyo |
| Consumption this week / this month / last month | calendar totals in Asia/Tokyo |
| Latest reading timestamp | when the newest interval ended |
| Data delay | how far behind the newest interval is |
| Status | whether the supply point is active or ended |
| Data available | whether readings are currently arriving |

A calendar total reports **unknown** until the whole period is covered, rather than
a number that is quietly too low. A partly-synchronised day is not a smaller day.

There is no "current power" entity. OEJP publishes 30-minute totals, not live power,
and presenting an average as instantaneous would be wrong.

### Per account

Enabled by default: account status, current product, current agreement start and
end.

Disabled by default, because they are financial: account balance, overdue balance,
latest bill amount, latest bill issued date, latest bill payment due date, latest
transaction amount. Enable individually from the entity settings if you want them.

## How data updates

| What | How often |
|---|---|
| Consumption | every 30 minutes, re-reading the last 72 hours |
| Account and supply-point discovery | every 24 hours |
| Contract and billing | every 12 hours |
| Full reconciliation | once a day, over the current and previous month |

**Readings arrive late.** OEJP publishes a 30-minute interval several hours after
it happens, sometimes longer. That is the provider's meter data pipeline, not the
integration. Nothing is missing; it has not been published yet.

**Readings get corrected.** When a billing period closes, OEJP reissues its
intervals with a different version, and the values can change. The integration
stores every interval rather than a running total, so a correction rewrites the
affected history and every later Energy Dashboard total, deterministically.

**Startup does not stall.** Setup completes from recent data; older history is
fetched in the background afterwards.

## Energy Dashboard

Add the integration's statistics under **Settings → Dashboards → Energy → Grid
consumption**, and pick the OEJP statistic for the supply point, not a period
sensor. The period sensors are for display; the statistics are the correction-safe
source.

Details are in [`ENERGY_STATISTICS.md`](ENERGY_STATISTICS.md), and there is a
Japanese page at [`ja/ENERGY_DASHBOARD.md`](ja/ENERGY_DASHBOARD.md).

## Known limitations

**No electricity cost.** OEJP publishes a per-interval `costEstimate`, and the
integration deliberately does not show it. Measured against a real invoice it uses
a simplified rate model that does not reproduce the billed tariff: it applies one
tier boundary where the tariff has two, and cannot express the fixed daily standing
charge at all. Showing it as your cost would carry provider authority for a figure
no line of your bill supports. The evidence is in
[`CONTRACT_AND_BILLING.md`](CONTRACT_AND_BILLING.md).

**No tariff unit prices.** OEJP returns rates keyed by an untyped provider payload,
with no reliable way to tell which one applies to your supply point.

**No `kWh × unit price` estimate.** A Japanese electricity bill combines tiered
energy pricing, a fixed daily charge, a monthly fuel-cost adjustment, a renewable
levy, and tax. One unit price cannot reproduce it.

**Roughly 30 days of provider history.** A first install starts with about a month.
Local history grows from there.

**Calendar totals are not billing periods.** Your bill runs to a meter-read time a
few hours after midnight; the integration's months are Asia/Tokyo calendar months.
The two will not match, by design.

**Contract information may be unavailable.** Some accounts are not authorised for
agreement data. Consumption is unaffected; those entities stay unavailable and a
repair message explains it.

## Troubleshooting

Start with **Settings → System → Repairs**. The integration raises a plain-language
message for each condition it can detect, and each says whether you need to do
anything. Usually you do not.

| Symptom | Likely cause |
|---|---|
| Setup says My Home Assistant is required | `my` is not loaded; add `my:` or restore `default_config:` and restart |
| Totals say **unknown** | the period is not fully covered yet; normal soon after install |
| Latest interval is hours old | normal OEJP publishing delay; check *Data delay* |
| Entities went unavailable together | a refresh failed; the integration retries automatically |
| Home Assistant asks you to reconnect | the OAuth authorization expired or was revoked |
| Financial entities missing | they are disabled by default; enable them individually |
| Energy Dashboard totals changed retroactively | OEJP corrected an interval; expected |

### Reporting a problem

Download diagnostics from the integration's menu and attach the file to a GitHub
issue. It contains **no** token, email address, account number, supply-point
number, address, reading value, or monetary amount, so you do not need to review it
first. Please do not paste Home Assistant logs, which can contain provider text.

What the file contains, and why, is in
[`DIAGNOSTICS_AND_REPAIRS.md`](DIAGNOSTICS_AND_REPAIRS.md), with a Japanese summary
at [`ja/DIAGNOSTICS.md`](ja/DIAGNOSTICS.md).

## Removing the integration

**Settings → Devices & services → Octopus Energy Japan → Delete.** The integration
asks OEJP to revoke its authorization, and deletes its stored readings, sync
checkpoints, and installation secret.

Two things survive on purpose:

- **Energy Dashboard statistics** stay in the Home Assistant recorder, so your
  energy history is not destroyed by removing an integration. Remove them under
  **Developer tools → Statistics** if you want them gone.
- **Application credentials** stay, so you can re-add the integration without
  re-entering the client ID. Remove them under **Settings → Devices & services →
  Application credentials**.

Re-adding starts a fresh local history and downloads whatever OEJP still serves,
roughly the last 30 days.

## Privacy

Your data stays in your Home Assistant. There is no external telemetry and no
developer-operated server. See [`../PRIVACY.md`](../PRIVACY.md).
