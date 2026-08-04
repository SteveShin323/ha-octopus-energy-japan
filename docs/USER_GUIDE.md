# User guide

This is the reference for people running the integration. It is the normative
English documentation; the Japanese version is [`ja/README.md`](ja/README.md).

> [!IMPORTANT]
> **The account sign-in is not available yet.** Octopus Energy Japan has not issued an
> OAuth client ID, and there is no self-service way to create one. Until it does, use
> the **email and password** method, which works today. Progress on the client ID is
> tracked in [`OAUTH_APPLICATION_STATUS.md`](OAUTH_APPLICATION_STATUS.md).

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
- keep long-term history. OEJP serves every interval since your supply started;
  the integration stores what it collects, so your history keeps growing.

It is **not** a billing tool. See [known limitations](#known-limitations).

## Installation

Add the repository to HACS, install **Octopus Energy Japan**, restart Home Assistant,
then **Settings → Devices & services → Add integration → Octopus Energy Japan**.

Setup opens by asking how you want to sign in.

## Choosing a sign-in method

| | Octopus Energy Japan account | Device code | Email and password |
|---|---|---|---|
| Available now | no, OEJP has issued no client ID | no, same client ID | **yes** |
| Where you sign in | on the OEJP website | on the OEJP website, from any device | in Home Assistant |
| Your password | never requested or stored | never requested or stored | **stored in Home Assistant** |
| Needs My Home Assistant | yes | no | no |
| Expected lifetime | the method OEJP is moving to | the same, no redirect needed | being withdrawn, see below |

**Octopus Energy Japan account** is the method to prefer once it works. Sign-in
happens on the OEJP website, in your browser, and the integration never asks for your
password or sees it. It needs a public OAuth client ID registered under **Settings →
Devices & services → Application credentials**, and no such ID exists yet. Choosing it
today stops with a message telling you to add a credential first, which is the correct
behaviour when there is none to add.

**Device code** is the same OAuth authorization as the account method, obtained
without a browser redirect. Home Assistant shows a short code, you open the OEJP page
on any device and approve, and setup continues on its own. Because there is no
redirect it needs no My Home Assistant and works on an instance with no public
address, which makes it the better of the two OAuth methods for most installations.
It needs the same client ID, so it is not available yet either. The endpoint itself is
live: OEJP documents `/device-authorization/` and it answers.

**Email and password** works today. It is the provider's older login, and OEJP has
already removed these fields from its published API schema while continuing to accept
them, so it can stop working without warning. When that happens Home Assistant asks
you to reconnect, and the account method will be the only option left.

Your email and password are stored in Home Assistant. That is not for convenience: the
provider's refresh token lasts seven days and renewing it does not extend that, so
nothing but the credential itself can sign in afterwards. What is stored, and where, is
in [`../PRIVACY.md`](../PRIVACY.md).

### Switching later, without losing history

When a client ID exists, open the integration's menu, choose to reconnect, and pick
the account or device-code method. The entry is promoted in place: your readings and
Energy Dashboard statistics are kept, and **the stored password is deleted**. You do
not delete and re-add. The same route moves an entry between any two methods.

### My Home Assistant, for the account method only

It is part of `default_config:`, so a normal installation already has it. Sign-in
returns through `my.home-assistant.io`, the one redirect address registered with OEJP,
which forwards the result to your own instance. If you run a stripped-down
configuration, add `my:` to `configuration.yaml`. The integration stops with an
explanatory message rather than letting OEJP reject the sign-in for a redirect address
it does not recognise. The email and password method has no redirect and does not need
this.

### Configuration parameters

No API key, no account number, no supply point. Everything else is discovered from the
account you sign in with.

| Parameter | Where | Required | Meaning |
|---|---|---|---|
| Email and password | setup → **Email and password** | for that method | your OEJP sign-in, stored so the integration can sign in again |
| OAuth client ID | Settings → Devices & services → **Application credentials** | for the account and device-code methods | the public client ID issued by OEJP. Not a secret. Leave the secret field empty |
| Enabled historical resources | integration → **Configure** | no | which ended accounts or supply points to keep reporting |

Active accounts and supply points are always enabled and cannot be turned off, so a
new supply point starts reporting on its own.

## Entities

Every entity belongs to a device: one per account, and one per supply point beneath
it. Names are ordinal — `OEJP supply point 1-1` — and contain no account number,
supply-point number, or address, so a screenshot or a pasted automation carries none.

**To tell which supply point a device is**, open the device page: it shows the
supply-point number (供給地点特定番号) as its serial number, and the account device shows
your account number. Those are the numbers OEJP prints on a bill. They stay off entity
IDs, states, attributes, and the diagnostics download.

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

**Settings → Dashboards → Energy → Grid consumption → Add consumption**, then pick the
statistic named after your supply point, for example `OEJP supply point 1-1 Import
energy`. If OEJP reports an export direction, add `… Export energy` under **Return to
grid**.

Pick the **statistic**, not a period sensor. The period sensors are for display; the
statistics are the correction-safe source, and they are what Home Assistant can rewrite
when OEJP revises a reading.

**Set cost to `OEJP supply point 1-1 Import cost`.** Leave the price fields alone — Home
Assistant only multiplies a *sensor* by a price, and skips that for an external statistic,
so anything you type there is ignored rather than applied. The cost statistic is the route,
and the integration publishes one.

It is computed from your own tariff as OEJP reports it: the three price steps with their
kWh boundaries, the daily standing charge, the monthly fuel-cost adjustment, and the annual
renewable levy. You enter none of it.

**Treat it as a good estimate, not your bill.** Measured against one real bill it came to
104% of the billed total, for two reasons that are unavoidable today. Your bill runs to a
meter read a few hours after midnight, while the price steps here restart on the Tokyo
calendar month. And OEJP publishes only the *current* month's fuel-cost adjustment, so
hours from earlier months are priced without one. The reasoning is in
[`ENERGY_STATISTICS.md`](ENERGY_STATISTICS.md).

There is deliberately no cumulative meter sensor to choose instead. OEJP publishes
30-minute totals hours late and revises them when a billing period closes; a cumulative
sensor fed from that would lag or jump backwards, and the dashboard would record both.

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

**Cost is an estimate, and history is incomplete.** The tariff itself is read from your
agreement, so the prices are yours rather than a guess. What limits it is the boundary your
bill uses and the fuel-cost adjustment's history, both described under
[Energy Dashboard](#energy-dashboard) above. The provider's own per-interval `costEstimate`
is still not published, because it collapses the adjustment and levy into one figure and
cannot express the standing charge — see
[`CONTRACT_AND_BILLING.md`](CONTRACT_AND_BILLING.md).

**No `kWh × unit price` estimate.** A Japanese electricity bill combines tiered
energy pricing, a fixed daily charge, a monthly fuel-cost adjustment, a renewable
levy, and tax. One unit price cannot reproduce it.

**A first install starts with the current and previous month.** That is the
integration's own initial sync, not a limit on what OEJP serves: measured on a real
account, every interval since supply started is still retrievable. One response is
capped at 1488 intervals, which is 31 days of half-hourly data, and the integration
requests seven days at a time to stay well inside it.

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
| Setup says My Home Assistant is required | `my` is not loaded; add `my:` or restore `default_config:` and restart. Affects the account method only |
| Asked to reconnect an email and password entry | your OEJP password changed, or OEJP stopped accepting password login |
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

**Settings → Devices & services → Octopus Energy Japan → Delete.** This deletes the
entry's stored readings, sync checkpoints, installation secret, and any stored
credential.

An account entry also asks OEJP to revoke its authorization. An email and password
entry cannot: OEJP does not let an account user invalidate a refresh token, so that
token expires on the provider's side within seven days instead. To cut it off at once,
change your OEJP password.

Two things survive on purpose:

- **Energy Dashboard statistics** stay in the Home Assistant recorder, so your
  energy history is not destroyed by removing an integration. Remove them under
  **Developer tools → Statistics** if you want them gone.
- **Application credentials** stay, so you can re-add the integration without
  re-entering the client ID. Remove them under **Settings → Devices & services →
  Application credentials**.

Re-adding starts a fresh local history and downloads the current and previous
month again.

## Privacy

Your data stays in your Home Assistant. There is no external telemetry and no
developer-operated server. See [`../PRIVACY.md`](../PRIVACY.md).
