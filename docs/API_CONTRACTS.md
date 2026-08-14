# API contracts

API behaviour that shaped the code and is not obvious from reading it. Every item here was
measured against a real account rather than taken from documentation. The detailed
reasoning for a given query lives in a comment beside that query; this page is the index
a contributor should read before changing a GraphQL document.

- GraphQL endpoint: `https://api.oejp-kraken.energy/v1/graphql/`
- Auth server: `https://auth.oejp-kraken.energy`
- Official documentation: `https://docs.oejp-kraken.energy/graphql/guides/`

## Request limits

Published in the official API guide, and the reason for the constants the code uses. A
test pins the relationship.

| Limit | Value | Error code |
|---|---|---|
| Pagination `first` | must be under 100 | — |
| Query complexity | 200 per request | `KT-CT-1188` |
| Nodes | 10,000 per request | `KT-CT-1189` |
| Hourly points | 50,000 for an account user | `KT-CT-1199` |

All three codes are classified as rate limits so they back off rather than being
mistaken for a schema or permission problem.

`first` is **required** on a connection, not optional. Omitting it returns `KT-CT-1201`,
which is a validation error about the missing argument — not an authorization failure.
Treating it as an authorization failure makes it look as though the account is not allowed
to read that field.

## Readings

**One response returns at most 1488 intervals — 31 days of half-hourly data — and drops the
oldest beyond that with no error and no flag.** An over-wide window therefore looks like
missing history. Requests use seven-day windows.

Measured on one account: the legacy `halfHourlyReadings` returns a fixed count and always
stops exactly 31 days back however wide the window, dropping the oldest rows silently. The
generic `readings` connection paginates and returned every interval of a 48-day window across
24 pages, so the cap binds a response rather than a range. **No retention limit was observed**
— intervals were present back to the start of that account's supply, which is 48 days. Whether
a longer-lived account can reach further back is untested; the code assumes only that what the
provider returns is what exists.

**A reading's `version` marks the billing lifecycle.** Intervals are reissued with a new
version when a period closes, and values change. This is why the ledger stores keyed
intervals rather than a running total, and why statistics are external rather than
recorder-backed.

**A billing period appears to run from one meter reading to the day before the next.** The
whole evidence is one account with one closed invoice, so treat it as a measurement rather than
a documented contract:

| Field | Reported | Used |
|---|---|---|
| `nextReadingDate`, `nextNextReadingDate` | both day 18, one month apart | **yes** — two agreeing dates are the recurring schedule stated twice |
| `supplyPeriods.supplyStartAt` | 2026-06-18 00:00 JST | **yes**, as the weaker fallback |
| the closed invoice | 6/18 to 7/17 | the thing both were checked against |
| `readingDateDayOfMonth` | 19 | no — agrees with neither the invoice nor either scheduled date |
| `bills.fromDate` / `toDate` | 6/17 to 7/22, issued 7/23 | no — the document's own period |
| `statements.startAt` / `endAt` | 6/17 to 7/23 JST | no — likewise, and a day out from `bills` |

The supply start is the weaker of the two used because it lands on the read day only when
service happened to begin on one. Both are `DateTime`s: `supplyStartAt` reads as the 17th until
converted to JST, which is a day's error if it is not.

`readingDateDayOfMonth` is rejected on one account's evidence, which is thin. It is published
as a diagnostic sensor and the diagnostics download reports whether it agrees with the derived
anchor, so a contradicting account can be recognised rather than guessed at.

**`StatementType.consumptionStartDate` and `consumptionEndDate` exist and are unverified.**
They would be the reading period stated outright. On the one account measured the newest bill
resolves as `PeriodBasedDocumentType`, so everything behind the `StatementType` fragment is
null and they could not be observed. An account whose documents do resolve as `StatementType`
would be better evidence than any derivation here.

Calendar day, week, and month totals still use Asia/Tokyo boundaries and will not equal an
invoiced period, which is a presentation choice rather than a defect.

**Asia/Tokyo is a property of the provider, not an assumption about a region.** Japan has one
timezone and no daylight saving, the provider bills in JST, and its rate validity windows were
observed as JST calendar months. Nothing in the integration assumes a service area, a grid
operator, or a plan shape: rates arrive scoped to the agreement, and `gridOperatorCode` and
`regionOfOperation` are read only to refuse a response that mixes two of them.

**Authorization can depend on the path a field is reached by, not only on the field.** The same
`supplyPeriods` selection returns data through `account(accountNumber:)` and
`AUTHORIZATION/KT-CT-4501` through `viewer.accounts`. Both were measured on the same account in
the same session. That is why the supply start is asked account-scoped rather than added to the
viewer document the resource discovery uses: a strict execution there turned one nulled optional
field into a failed setup. Do not conclude that a field is unavailable to an account from one
path alone.

**The market name needs a territory prefix.** `JPN_ELECTRICITY`, not `ELECTRICITY`.

## Tariff

The whole tariff is readable by an account user on the agreement's product:
consumption charges with their kWh step boundaries, `standingChargePricePerDay`, the
monthly fuel-cost adjustment with the period it covers, and the annual renewable levy.

**It is reached through an inline fragment on a union.** `Agreement.product` is declared
as the union `Product`; no field anywhere returns `ProductInterface` or one of the
concrete product types. Searching the schema for those type names finds nothing, and
this repository concluded three separate times that the tariff was unreachable before
someone tried `... on ElectricitySteppedProduct`.

Read it through `ElectricitySupplyPoint.agreements`, not through
`marketSupplyAgreements`: the latter's `product.rates` is refused, and because GraphQL
propagates an error to the nearest nullable parent, the refusal nulls the whole product
with it.

**The standing charge is already resolved for the contract.** `standingChargeUnitType`
reads `YEN_AMPERE_DAY`, which describes how the charge is *determined* — by contracted
amperage, per day — and not the unit of the number returned. On the one account measured,
`standingChargePricePerDay` equalled the per-day amount its published tariff table lists
for that account's contracted amperage, and the same amount appeared on its invoice as the
daily basic charge. A per-ampere rate would have been that figure divided by the amperage.
So it is used as a daily amount, which is what this integration already did; what changed
is that it is now established rather than assumed.

That also settles why the contracted amperage is not needed to price anything.

## Time-of-use bands say what, never when

A tariff priced by time of day returns its bands and prices on the same
`consumptionCharges` — `band: "CONSUMPTION_03_DAY"`, `timeOfUse: "EV_DAY_TIME"`, a price — and
never the hours those bands cover.

**The one field that would answer it is refused.** `Query.rateGroupTouScheme` returns
`KT-CT-1111 Unauthorized` for every argument, including the real scheme identifier, on two
accounts — one on a stepped tariff and one actually on the EV tariff. The documented argument
errors (`KT-CT-12010`, `KT-CT-12049`) and the disabled-field error (`KT-CT-1113`) never appear,
so the refusal happens before the arguments are resolved. `full-customer-access` does not open
it. A full introspection of the 2290-type schema found it to be the only field returning
`TimeOfUseSchemeType`, and the alternative route to the hours — `VariantProfile.schemes` — is
reachable only through `availableProducts` or `agreementsForRollover`, both equally refused.

**`tariffSummary` is open, and names the schedule.**
`Query.tariffSummary(gridOperatorCode!, productCode)` needs no entitlement to the product; with
`productCode` omitted it returns the whole catalogue for the area. Its `productParams` carries
`time_of_use_scheme`, the provider's own identifier for the schedule. The same blob is on the
agreement's product as `params`, which is read first; `tariffSummary` is the fallback when it
arrives empty.

**Time of use and steps never combine.** Every time-of-use product in every grid area reports
its consumption rates with `stepStart` and `stepEnd` null. A tariff charges by the hour or by
cumulative kWh, never both.

**Bands are self-describing.** A band reads `CONSUMPTION_{grid operator}_[HIGH_|LOW_]{slot}`.
The `HIGH_`/`LOW_` marker appears in areas 06, 07 and 08 and matches `contractCapacityPattern:
TIERED_HIGH`/`TIERED_LOW`; it selects a price, not a schedule.

The hours themselves are transcribed from the provider's published tariff documents into
`api/tou.py`. [TOU_SCHEMES.md](TOU_SCHEMES.md) holds the transcription and its sources.

## Contracted capacity cannot be read

Nothing an account user can reach reports the contracted capacity, on the one account
measured:

| Field | Result |
| --- | --- |
| `ElectricitySupplyPoint.contractedCapacity { value unit }` | `AUTHORIZATION` / `KT-CT-4501`, through `account(accountNumber:)` and through `viewer.accounts` alike |
| `ElectricitySupplyPoint.contractedCapacityOld` | the same refusal on both paths |
| `ElectricitySupplyPoint.supplyDetails { amperage kva }` | `null`, with no error |
| `ElectricitySupplyPoint.meters` | an empty list, so `ElectricityMeter.capacity` never arrives |

This is one of the few refusals that is *not* path-dependent, so it is a permission the
account user does not hold rather than a wrong query. `meters { serialNumber capacity }`
used to be selected and parsed into a model nothing ever read; it was removed once the
list turned out to be empty in practice. Nothing here is needed to compute a cost — see
the standing charge above.

## Provider cost is not published

The legacy API returns a per-interval `costEstimate`. It is stored but never shown.
Measured against a real invoice it applies one tier boundary where the tariff has two,
collapses the fuel-cost adjustment and the renewable levy into a single constant, and
cannot express the daily standing charge at all. The integration computes cost from the
tariff instead.

## Billing

**Transactions are on the ledger, not on the account.** `Account.transactions` exists,
is readable, and returns an empty connection. `LedgerType.transactions` returns the
customer's activity.

**The payment due date is on the ledger's statement.** The newest `bills` node resolves
as `PeriodBasedDocumentType` while reporting `billType: STATEMENT`, so every field behind
`... on StatementType` — `paymentDueDate` and `status` among them — resolves to nothing.
`LedgerType.statements` returns the same document as `StatementBillingDocumentType`,
which carries `dueDate`. The two share an id and are matched on it; ordering needs
`FINALIZED_AT_DESC`, because the connection's first node is otherwise not the newest.

Both of these shipped as permanently empty sensors, because the test fixtures used the
shape the account-level field would have returned. Regression tests now pin why the more
direct-looking field is not used.

## Authentication

**An expired access token is reported as an application error**, `KT-CT-1124` with
errorType `APPLICATION` and the message "Signature of the JWT has expired." Neither the type
nor the message shape says authentication, so the code is the only signal — and getting that
wrong is expensive, because refreshing happens only for an authentication error and a stored
token is never checked for age.

**A rejected credential is reported as a validation error**, `KT-CT-1138` with
`errorType: VALIDATION`, not as an authentication error. Classification keys on the code
before the type for exactly this reason; without that ordering an expired authorization
and a wrong password would be handled the same way.

**The password fields are absent from the published schema but still accepted.** The
login can therefore stop working without a schema change, so a rejected sign-in is
terminal and raises reauth rather than retrying.

**Token lifetimes.** The access token lasts one hour. The refresh token lasts seven days,
is not rotated on use, and renewing it does not extend the expiry — which is why the
password method must store the credential. Long-lived refresh tokens are restricted to
third-party organizations. An account user cannot invalidate a refresh token; the
mutation exists and is refused.

## Optional commercial operations

Account status, agreements, and billing are three independent documents. Each records its
own availability, so an account not authorised for agreement data still reports
consumption. Authorization refusal (`forbidden`), schema absence (`unsupported`), and
transport failure (`failed`) are distinguished; authentication errors propagate instead
of becoming an availability status.

## Coverage is deliberately scoped

A field-by-field probe of every reachable type found considerably more readable data than
the integration publishes. Most of it stays unpublished:

| Reason | What it covers |
|---|---|
| About the customer, not the supply | health, hardship, contact details, payment instruments, consents |
| An affordance for a mutation this integration does not perform | anything describing an action available in the Octopus Energy Japan app |
| Already published under another name | account-level payments duplicate the ledger transaction that is published |
| Measured to be unmaintained | the two "next reading date" fields were both in the past and disagreed with the reading day on the same supply point |
| Undecidable on the account available | four balance fields agreed, but all were zero; the account's only ledger reports `affectsAccountBalance: true`, so its balance is a component of the account balance already published |
| Not worth an entity's cost | a signed URL that would exceed the state length limit; fields that were null, zero, or equal to one already published |

The zero-balance row needs care. The four fields agreed, which looked like redundancy, but
they agreed only because all four were zero. Comparing them again on an account with a
non-zero balance is the way to settle it.

Publishing a field from any of these categories is a decision, not an omission to be
corrected. Revisit them individually.

## One account shape is measured; the rest are reasoned

Everything above was read from a single real account: **one account, one property, one
electricity supply point, electricity only, one consumption agreement in force.** That shape
is verified end to end, by `tests/test_live_account.py` on every change.

Every other shape this integration claims to handle is exercised only by hand-written
payloads. Those are how two of this project's eight "looks implemented, returns nothing"
defects survived — a fixture that agrees with the parser proves the parser agrees with
itself. Only three contract fixtures are derived from real responses, and all three are
readings.

| Shape | Handled by | Evidence |
| --- | --- | --- |
| 1 account / 1 property / 1 point, electricity | the whole integration | **a real account** |
| Two logins, one config entry each | connection labels in device names | **two real accounts** |
| An entry whose only agreement has ended | lifecycle, opt-in reporting | **a real account** |
| A tariff priced by time of day | `api/tou.py`, band pricing | a real response, quoted in [issue #93](https://github.com/SteveShin323/ha-octopus-energy-japan/issues/93) |
| Several accounts on one login | discovery, ordinal device names | hand-written |
| Several properties on one account | discovery | hand-written |
| Several supply points on one property | discovery, per-point ledgers and statistics | hand-written |
| Historical (moved-out) account or supply point | lifecycle, opt-in reporting | hand-written |
| Several supply periods, with a gap | earliest billable period anchors the billing period | hand-written |
| No electricity supply points at all | discovery returns an account with none | hand-written |
| A gas supply point beside electricity | not queried; `GasTieredProduct` is not a consumption product here | none |
| Readings only through the legacy path | capability detection; the history walk refuses | hand-written |
| Every consumption agreement ended | reported as a repair issue | hand-written |
| A single non-revoked consumption agreement has ended, none live | priced as an estimate, `SupplyPointTariff.is_estimate` | **a real account** |
| Two or more ended agreements, none live | refused (`AGREEMENT_HISTORY_UNSUPPORTED`); would price one tariff over hours that belong to another | hand-written |

Treat "hand-written" as unverified against reality, not as covered. The way to settle any
row is a real account with that shape, and until one exists the honest statement is the one
in this table.
