# OEJP API contracts

This document records the discovery contracts used by the integration. The
official OEJP schema exposed to an authenticated customer remains the source of
truth. Sanitized probe fixtures must be regenerated when that schema changes.

## Reading contract provenance

The reading documents were validated on 2026-07-29 against the public
introspection schema and GraphQL validation endpoint at
`https://api.oejp-kraken.energy/v1/graphql/`. Validation reached the expected
`KT-CT-1112` authorization boundary without a schema or document error for all
six generic target/direction variants and both legacy operations. This proves
the public document shape, not customer OAuth permission or the presence of
data for a particular supply point.

The official
[OEJP GraphQL changelog](https://docs.oejp-kraken.energy/graphql/changelog/)
remains authoritative. In particular:

- `SupplyPointType.readings` was introduced in May 2025;
- import/export connections and device/register reading scopes replaced the
  original connection shape in October 2025;
- the `units` filter was added in January 2026;
- `timeGranularity` became nullable in February 2026; and
- the current `qualities` metadata replaced the removed singular quality
  fields in May 2026.

Protected customer schema and responses must still be confirmed with the
redacting local probe before alpha release.

## Resource discovery

The legacy customer hierarchy provides:

```text
viewer
└── accounts
    └── properties
        └── electricitySupplyPoints
            └── meters
```

The parser never selects the first account, property, supply point, or meter.
Every resource is retained in a deterministic typed hierarchy. Provider status
strings are normalized to `active`, `historical`, or `unknown`; unknown values
are not guessed.

## Generic device discovery

When introspection confirms that `Query.supplyPoint` and
`SupplyPointType.devices` are available, each discovered electricity supply
point is queried by `externalIdentifier` and the `JPN_ELECTRICITY` market. Generic
devices use `deviceIdentifier`; their registers use `registerIdentifier`.

Generic discovery is optional. An authorization or schema capability failure
does not invalidate working legacy discovery. Authentication, rate-limit,
transport, and malformed-response failures are not treated as capability
fallbacks.

## Capability registry

Capability introspection distinguishes:

- `supported`: the required root and object fields are visible;
- `unsupported`: introspection succeeded and a field is absent;
- `forbidden`: an authorization error prevented a reliable observation;
- `unknown`: the capability has not been probed.

Authorization is never classified as authentication and must not trigger OAuth
reauthentication.

## Published request limits

The official API basics guide states these limits. They are the reason for the
constants the integration uses, and a regression test pins the relationship:

| Limit | Published value | Error code | Integration |
|---|---|---|---|
| Pagination `first` | must be less than 100 | — | `GENERIC_PAGE_SIZE` is 99; every connection requests at most 99 |
| Query complexity | at most 200 per request | `KT-CT-1188` | documents stay shallow and request one connection at a time |
| Node limit | at most 10,000 nodes per request | `KT-CT-1189` | the largest observed response was under 1,500 nodes |
| Hourly points | 50,000 for an account user, 300,000 for an OAuth application | `KT-CT-1199` | 30-minute polling with a 72-hour overlap, 24-hour discovery, 12-hour commercial |

All three error codes are classified as rate limits, so they back off rather than
being mistaken for schema or permission problems.

## Pagination safety

Relay-style connections are collected with `hasNextPage` and `endCursor`.
Missing cursors, repeated cursors, and an excessive number of pages fail
closed. A caller must not infer completion from `hasPreviousPage`.

## Generic reading provider

The preferred provider calls `readings` at the most granular discovered level:

```text
DeviceRegister.readings
  -> Device.readings
  -> SupplyPointType.readings
```

Registers are preferred over their parent device and supply-point totals so the
same physical energy is not represented at multiple aggregation levels.
Import and export are fetched as separate Relay connections with `first <= 99`
and guarded cursor pagination.

The request contract is:

- `readingType: INTERVAL`;
- `timeGranularity: THIRTY_MIN`;
- `timezone: "UTC"`;
- energy units restricted to watt-hours, kilowatt-hours, and megawatt-hours;
- `intervalStart`, `intervalEnd`, `value`, and `units` required in every node;
  and
- `qualities { quality value count }` requested only when capability discovery
  confirms the field.

Provider timestamps are normalized to UTC and numeric values remain
`Decimal`. Quality entries are sorted deterministically. Conflicting duplicate
intervals in one response fail closed.

Quality metadata is optional. A structured authorization error whose path is
confined to `qualities` causes one retry without that field. Unscoped
authorization errors are not downgraded.

## Legacy reading provider

`halfHourlyReadings` is queried by account and bounded datetime range. The
parser retains `startAt`, `endAt`, `value`, `version`, and the OEJP-issued
`costEstimate`. Values are treated as kWh and, unless discovery identifies an
export series, as imported consumption.

`intervalReadings` is queried separately and retained under a distinct source.
Those records are billing-period aggregates and must never be added directly
to overlapping half-hour readings. The ledger and aggregation layer owns that
reconciliation rule.

## Strict provider fallback

Generic-to-legacy fallback is permitted only for:

- an observed unsupported or forbidden generic capability;
- a structured OAuth authorization gap scoped to a generic reading child
  field;
- `KT-CT-1113` or an equivalent structured disabled-field type; or
- a null/unconfigured generic device, register, or reading series after the
  requested supply point was found.

Authentication, rate limits, timeout/network/server failures, malformed data,
not-found identifiers, and unrecognized validation errors remain visible and
never trigger fallback.

Runtime authorization fallback is restricted to errors whose GraphQL path is
scoped to `readings`, `importReadings`, `exportReadings`, `devices`, or
`registers`. Missing-authentication (`KT-CT-1112`), account-authorization
(`KT-CT-4177`), unscoped authorization errors, and a null root `supplyPoint`
are propagated instead of being hidden by legacy fallback.

Every provider result records the selected provider, an allow-listed fallback
reason, the exact queried series (including series with zero returned
intervals), and the source families replaced by that successful batch. This
metadata lets the ledger delete stale topology and provider-transition rows
without guessing authority from a non-empty response.

## Credential rejection is reported as a validation error

Confirmed against a real account on 2026-08-04: `obtainKrakenToken` with an
incorrect password returns `errorType: VALIDATION` with
`errorCode: KT-CT-1138`, and the same document with the correct password returns
a token. OEJP therefore does not use `AUTHENTICATION` for a rejected credential.

Classification keys on the code before the type for exactly this reason. Without
that ordering the integration would treat an expired or revoked authorization as
an unsupported schema and mark the capability permanently unavailable instead of
requesting reauthentication.

## Market name requires a territory prefix

`Query.supplyPoint(externalIdentifier: String!, marketName: String!)` validates
the market name as a plain string, not an enum, so introspection cannot enumerate
the accepted values. Confirmed empirically on 2026-08-04:

| Value | Result |
|---|---|
| `JPN_ELECTRICITY` | accepted, resolved the supply point |
| `ELECTRICITY` | rejected, `KT-CT-4723` |
| `JP_ELECTRICITY` | rejected, `KT-CT-4723` |
| `JAPAN_ELECTRICITY` | rejected, `KT-CT-4723` |
| `JPN_GAS` | reached authorization (`KT-CT-1111`), not a format error |

`JPN_GAS` failing on authorization rather than format is what confirms the
`TERRITORY_MARKETNAME` pattern rather than a single magic string.

Before this was confirmed the integration sent `ELECTRICITY`, so every generic
readings and generic device request failed validation. The failure classified as
an unsupported capability and the runtime fell back to legacy readings
permanently, which is why consumption still worked while import/export
separation, device and register scopes, and reading quality metadata never did.
`ELECTRICITY_MARKET_NAME` in `api/models.py` is now the single definition.

## Legacy readings cap one response at 1488 intervals and truncate silently

Measured on a real account 2026-08-04, holding the window end fixed at
`2026-08-03T00:00Z` and moving only the start:

| Window | Intervals requested | Intervals returned | Oldest returned |
|---|---|---|---|
| 29 days | 1392 | 1392 | 2026-07-05 |
| 30 days | 1440 | 1440 | 2026-07-04 |
| 31 days | 1488 | 1488 | 2026-07-03 |
| 32 days | 1536 | **1488** | 2026-07-03 |
| 40 days | 1920 | **1488** | 2026-07-03 |
| 47 days | 2256 | **1488** | 2026-07-03 |

The cap is exactly **1488 intervals**, which is 31 days of half-hourly data. Beyond
it the provider **keeps the newest intervals and silently drops the oldest**: no
error, no warning, no pagination marker, and the returned start is later than the
requested start.

It is not a node-count limit. Selecting one field per interval instead of five
returned the same 1488, so the documented 10,000-node limit is not what binds here.

It is a **result cap, not a history horizon.** Seven-day windows walked back to the
supply-start date each returned their full 336 intervals, including a partial 258
for the first week of supply, and the window before supply began returned zero. All
history since supply started is therefore retrievable, provided each request stays
inside the cap. The returned blocks had no duplicate intervals and no gaps.

`MAX_QUERY_WINDOW` is seven days, so the integration never approaches the cap. The
consequence for ledger authority, and the invariant that keeps it safe, are
recorded in [`LEDGER_AND_AGGREGATION.md`](LEDGER_AND_AGGREGATION.md).

User-facing documentation stated for a while that OEJP serves "roughly the last 30
days". That was this cap mistaken for a retention limit, and it is false: the 31-day
figure is how much fits in one response, not how much exists.

## The legacy login: lifetimes, renewal, and what a customer may not do

Measured on a real account 2026-08-04, running the shipped operations:

| Property | Value |
|---|---|
| Access-token lifetime | 1 hour |
| Refresh-token lifetime | 7 days |
| Refresh token rotated on renewal | no; the previous one stays valid |
| Refresh-token expiry extended on renewal | no, `+0s` |
| Renewal needs the password | no, `{refreshToken: ...}` alone is accepted |
| Renewed token resolves the same `viewer.id` | yes |

Renewal therefore buys at most seven days from one sign-in, which is why the password
method has to store the credential. See
[ADR 0008](adr/0008-password-authentication.md).

`ObtainJSONWebTokenInput`'s introspected fields are `APIKey`, `organizationSecretKey`,
`preSignedKey`, `refreshToken`, and `captchaResponse`. **`email` and `password` are
absent yet still honoured.** A hidden-but-honoured field can be withdrawn without a
changelog entry, and none appeared between 2026-05-18 and 2026-08-04.

`obtainLongLivedRefreshToken` is documented by the provider as "limited to authorized
third-party organizations only. Account users can only generate short-lived refresh
tokens", so there is no long-lived option for a customer.

`invalidateRefreshToken` exists but returned `AUTHORIZATION/KT-CT-1111` when called as
the signed-in account user, with the provider documenting `KT-CT-1111` and
`KT-CT-1130` Unauthorized for it. **A customer cannot revoke their own refresh token**,
so the integration does not try; the token expires on its own.

Its payload field `token` is a `RefreshToken` object exposing `expiryDt`, `key`, and
`isValid`, not a scalar. Selecting it without a selection set is rejected with HTTP 400
before the request reaches the schema.

## Documented API limits

From the provider's GraphQL guide, read 2026-08-04:

| Limit | Value | Error when exceeded |
|---|---|---|
| Paginated `first` argument | required, and **less than 100** | request errors |
| Query complexity | 200 per request | `KT-CT-1188` |
| Nodes per request | 10,000 | `KT-CT-1189` |
| Request rate | static or dynamic per operation | `KT-CT-1199` |

Hourly complexity-point allowances differ by caller, which is why the
authentication method affects headroom rather than only privacy:

| Caller | Points per hour |
|---|---|
| Account user | 50,000 |
| Organization | 100,000 |
| OAuth application | 300,000 |

An email/password login is an account user, so it has one sixth of the allowance an
OAuth application gets. `MAX_PAGE_SIZE` in `api/models.py` is the single definition
of the page size, and `tests/test_api_conformance.py` scans every shipped query to
assert each connection requests it. `devices` and `registers` shipped without
`first` at all until that scan was added.

## Only the legacy API carries provider cost

`SupplyPointType.readings` returns `intervalStart`, `intervalEnd`, `value`,
`units`, and `qualities`. There is **no cost field**. Provider-issued
`costEstimate` exists only on the legacy `halfHourlyReadings` and
`intervalReadings` operations.

The generic API is the preferred provider, so any future provider-cost feature
depends on the legacy operations that the fallback policy deliberately restricts.
That coupling has to be resolved before provider cost is published.

Generic readings paginate at the requested `first` value and report
`hasNextPage` with an `endCursor`, walking backwards from the window end, so a
seven-day window needs four pages at 99 per page.

## Reading version marks the billing lifecycle

The same probe returned two `version` values, and the boundary is exact:

| Version | Observed window (JST) |
|---|---|
| `MONTHLY` | up to and including the final interval of the closed billing period |
| `DAILY` | from the first interval of the open billing period onward |

`MONTHLY` covered the closed billing period and stopped at JST midnight after its
last day; `DAILY` began there. `version` therefore distinguishes a provisional
daily estimate from a figure finalized when the period is billed, so an interval's
value and cost can be reissued after the fact. That is the correction the interval
ledger exists to absorb, now observed rather than assumed.

The version boundary sits at JST midnight, while the invoiced period runs a few
hours further to the meter read, so the two are close but not identical.

## Provider cost is denominated in yen and excludes fixed charges

`costEstimate` was reconciled against a real invoice for the same supply point.

The implied unit price landed at 31.14 JPY per kWh for the closed period, inside
the 24.4 to 34.7 JPY per kWh band the invoice itself spans. A sub-yen minor unit
would have implied roughly 0.31, and a hundred-yen unit roughly 3114, so the
denomination is **whole yen with two decimal places**. Integer monetary fields
such as `balance`, `overdueBalance`, and `grossTotal` are whole yen for the same
reason: JPY has no circulating sub-unit.

`costEstimate` is **not** the billed amount, and its formula does not reproduce
the billed tariff. Reconstructing it over one gap-free billing period, against the
published tariff definition for the customer's menu, gives:

```text
costEstimate per kWh = marginal energy rate + a constant of about 8.47 JPY
```

where the marginal energy rate steps from the tariff's **first**-tier price to its
**second**-tier price at 300 kWh of cumulative period usage. Two independent checks
confirm the reading:

- the observed step is within 0.5 percent of the tariff's 120 kWh step and nothing
  like its 300 kWh step; and
- solving for the constant on that tier pair gives the same value from both bands,
  while the second and third tiers give inconsistent values.

So the provider applies the tariff's first tier progression at the wrong threshold,
skips the 120 kWh boundary entirely, and never reaches the third tier. The constant
also exceeds the tariff's fuel-cost adjustment plus renewable levy by about 1.10
JPY per kWh, and the fixed daily standing charge cannot appear in a per-interval
value at all.

Summed interval **values**, by contrast, reconcile: they reach the invoiced kWh once
the window is extended past JST midnight to the meter-read time, which is where the
billing period actually ends.

`costEstimate` is therefore a provider estimate computed from its own simplified
rate model. It must never be presented as the customer's cost.

## Billing periods end at the meter read, not at midnight

The invoiced period boundary is a meter-read instant a few hours after JST
midnight, as the tariff definition states. Summed intervals match the invoiced kWh
only when the window is extended to it.

Calendar projections deliberately use Asia/Tokyo day, week, and month boundaries
instead, so a monthly total will never equal an invoiced period. That is a
presentation choice, not a defect, and it is another reason period sensors are not
Energy Dashboard authority.

## Optional commercial operations

Account status, agreements, and billing are three separate optional documents,
not one combined query. `AccountCommercialOverview` reads `account` status and
balances. `AccountCommercialAgreements` reads `marketSupplyAgreements` with its
product and rate connection and follows `endCursor` pagination.
`AccountCommercialBilling` reads the single newest `bills` node, using inline
fragments for `StatementType`, `PeriodBasedDocumentType`, and `InvoiceType`, plus
the single newest transaction **from each of the account's ledgers**.

**The payment due date is on the ledger's statement, not on the bill.** On a real
account the newest `bills` node resolves as `PeriodBasedDocumentType` while
reporting `billType: STATEMENT`, so every field behind `... on StatementType` —
`paymentDueDate` and `status` among them — resolves to nothing. The
`latest_bill_due` sensor was therefore permanently empty, in the same way and for
the same kind of reason as the transaction sensor. `LedgerType.statements` returns
the same document as `StatementBillingDocumentType`, which does carry `dueDate`.
The two share an id, so they are matched on it rather than by assuming the newest
node of each connection corresponds; the bill's id is an `ID` and the statement's
an `Int`, so the comparison is textual. `statements` is ordered
`FINALIZED_AT_DESC`, because the connection's first node is otherwise not the
newest.

`status` is deliberately left absent for these documents. Nothing recovers it:
`documentDebtPosition` is null and `StatementBillingDocumentType.isFinal` is null,
both measured on 2026-08-04. A guessed settled/outstanding value would be worse
than none.

The bill *amount* needed no change, and the check that established this is worth
recording because an earlier comparison suggested otherwise. `PeriodBasedDocumentType`
and `StatementBillingDocumentType` report identical `totalCharges` — net, tax and
gross — on the same document, and the customer's cleared payment settles exactly
that gross. The earlier "the totals differ" reading came from comparing against
`totalCharges` on a fragment that had not been requested for the type that was
returned, so the field was simply absent.

**Transactions live on the ledger, not on the account.** `Account.transactions`
exists, is readable, and returns an empty connection. `LedgerType.transactions`
returns the customer's actual activity. Measured on 2026-08-04 against a real
account: `account.transactions` gave zero edges while the single ledger gave three
— a payment, a charge and a credit, each with a posted date. The integration read
the account-level field until then, so the latest-transaction sensor was
permanently empty. `tests/test_api_commercial.py` pins this so the more
direct-looking field is not restored. An account may hold several ledgers, so each
is asked for its own newest node and the newest overall is published; a nulled
`ledgers` is reported as no transaction rather than as an error, because that is
the shape of a partial response and refusing it would discard the bill that
arrived with it.

These documents were validated by schema introspection against a real account on
2026-08-04, which corrected three mistakes in the originally documented shape.

**Type names.** The account type is `Account`, not `AccountType`; `viewer` returns
`AccountUser`; `viewer.accounts` returns `[AccountInterface]`, so account fields
beyond the interface require `... on Account`.

**Rates.** `SupplyProductType.rates` returns `[ApplicableRateType!]!`, whose real
fields are `sourceSystem`, `name`, `pricePerUnit`, `unit`, `unitDisplay`,
`variantProfile`, `rateId`, `overridePrice`, `currency`, `category`,
`validityPeriod`, and `isSalesTax`. None of `gridOperatorCode`,
`regionOfOperation`, `band`, `validFrom`, `validTo`, `unitType`, or
`durationMonths` exists. Rate attribution is expressed through `category` and
`variantProfile`; `variantProfile` is `JSONString`/`GenericScalar`, so it is
opaque provider JSON rather than a typed contract.

**An account user may not read them, and asking is not free.** Measured 2026-08-04:
requesting `product.rates` returns `AUTHORIZATION/KT-CT-1111` at
`account.marketSupplyAgreements.edges.0.node.product.rates`. GraphQL propagates a field
error to the nearest nullable parent, so the whole `product` came back `null` and the
current product name was lost — for a field this integration never publishes. The same
query without `rates` returns no error at all, resolves the product, and moves the
`agreements` capability from `partial` to `available`. `ACCOUNT_AGREEMENTS_QUERY`
therefore omits it, and a test asserts it stays omitted.

**The whole tariff is on the agreement, and every earlier claim to the contrary was
wrong.** Two of them, made on 2026-08-04 and corrected the same day, came from searching
for fields whose *declared* type was `ProductInterface`, `ElectricitySteppedProduct`, or
`ElectricitySingleStepProduct` and finding none. The declared type is neither: it is the
union `Product`, and those are its members. Searching by member name could not find it.

`ElectricitySupplyPoint.agreements[].product` resolves to that union. Inside
`... on ElectricitySteppedProduct` everything a Japanese bill needs is present and
readable by an account user. Measured 2026-08-04:

| Field | Meaning | Observed |
|---|---|---|
| `consumptionCharges[]` | stepped energy price | three steps, `stepStart`/`stepEnd` at 120 and 300 kWh, `pricePerUnit` and `pricePerUnitIncTax` |
| `standingChargePricePerDay` | the standing charge **that applies** | one value; no amperage lookup needed |
| `standingChargeUnitType` | its unit | `YEN_AMPERE_DAY` |
| `fuelCostAdjustment` | monthly 燃料費調整 | `pricePerUnit`, `pricePerUnitIncTax`, and a `validFrom`/`validTo` covering one month |
| `renewableEnergyLevy` | annual 再エネ賦課金 | same shape, validity covering one year |
| `consumptionCharges[].validFrom` | when the price took effect | present on every rate |

`standingChargePricePerDay` matched the amperage that the `newAmperageOptions` set
difference identified, independently confirming both. The amperage inference is therefore
unnecessary: the agreement states the charge directly.

The fuel adjustment and levy together matched, to two decimal places, the constant that a
real invoice had earlier shown `costEstimate` to embed. Those two are what
`costEstimate` collapses into a single adder, and they are exactly what it cannot express
separately.

So a bill is reproducible from provider data alone, with no customer input:

```
hourly cost = kWh × price of the step the month's cumulative kWh falls in
            + kWh × (fuelCostAdjustment + renewableEnergyLevy)
            + standingChargePricePerDay ÷ 24
```

### Two catalogue queries also work, and are not needed

`tariffSummary(gridOperatorCode:, productCode:)` returns the same stepped rates plus a
standing charge for every contract amperage, and is permitted where
`marketSupplyAgreements → product.rates`, `agreementRates`, and `availableProducts` are
all refused with `AUTHORIZATION/KT-CT-1111`. `gridOperatorCode` is the first two digits of
the SPIN; `productCode` comes from the agreement. It is recorded because it is a working
path, but the agreement is the better source: it states the charge that applies rather
than a table to choose from.

### What an account user cannot read

Confirmed by calling every field of each type individually — 165 calls in the first pass
and 200 in the second — rather than inferring from the schema:

| Field | Result |
|---|---|
| `ElectricitySupplyPoint.halfHourlyReadings` | `KT-CT-4501` |
| `ElectricitySupplyPoint.intervalReadings` | `KT-CT-4501` |
| `ElectricitySupplyPoint.contractedCapacity`, `contractedCapacityOld` | `KT-CT-4501` |
| `ElectricitySupplyPoint.supplyPeriods` and the six move/change process fields | `KT-CT-4501` |
| `SupplyPointType.devices` | `KT-CT-7899` |
| `charges(accountNumber:, billingDocumentIdentifier:)` | `KT-CT-3824` |
| `Account.bill`, `paginatedPaymentForecast` | `KT-CT-4148`, `KT-CT-3949` |
| `Account.notes`, `canModifyPayments`, `debtCollectionProceedings` | `KT-CT-1111` |
| `PropertyType.parent`, `ancestors`, `descendants` | `KT-CT-1111` |

### `KT-CT-1201` means a missing `first`, not a refusal

The first coverage pass reported `KT-CT-1201` for `bills`, `transactions`,
`marketSupplyAgreements`, `payments`, and eighteen other connections, and reading that as
"unauthorized" would have been wrong. Supplying `first: 1` makes all of them answer
normally. The provider requires the pagination argument its guide documents, and reports
its absence with this code.

### `halfHourlyReadings` became unauthorized mid-session

Recorded because it bears on any cost work that depends on the legacy operations. On
2026-08-04 the legacy `halfHourlyReadings` field answered normally for many windows, and
later the same day began returning `AUTHORIZATION/KT-CT-4501` for **every** window,
including one that had just succeeded. At that moment
`rateLimitInfo.pointsAllowanceRateLimit` reported 750 of 50,000 points used and
`isBlocked: false`, and `fieldSpecificRateLimits` reported no entries at all, so it is
not the documented points allowance and not a field-specific limit.

The runtime is unaffected: the generic `SupplyPointType.readings` provider is preferred
and was the source of every stored reading. But it means the legacy operations cannot be
assumed available, which removes `costEstimate` — legacy-only — as a dependable cost
source independently of whether it reproduces the bill.

**Bill fragments.** `bills` returns `BillConnectionTypeConnection` over
`BillInterface`, implemented by `StatementType`, `PreKrakenBillType`,
`PeriodBasedDocumentType`, `CollectiveBillType`, and `InvoiceType`. Those
implementations disagree on nullability and on the total type:

| Field | `PeriodBasedDocumentType` | `InvoiceType` | `PreKrakenBillType` |
|---|---|---|---|
| `isHeld` | `Boolean!` | `Boolean` | absent |
| `isAnnulled` | `Boolean!` | `Boolean!` | absent |
| `totalCharges` | `StatementTotalType` | `InvoiceTotalType` | absent |
| `grossAmount` | absent | `Int` | `BigInt` |

GraphQL rejects one shared response name whose shapes differ, so the query aliases
these per fragment. `StatementType` carries `paymentDueDate`, `status`, and
`heldStatus` rather than `isHeld`.

Transactions return the `TransactionType` interface, implemented by `Charge`,
`Payment`, `Refund`, and `Credit`. Every field the integration selects exists on
the interface itself, so no transaction fragment is required.

The remaining unverified items are recorded in
[`CONTRACT_AND_BILLING.md`](CONTRACT_AND_BILLING.md).

Each operation runs through the optional execution path. A per-operation
availability status distinguishes provider permission (`forbidden`) from schema
rejection (`unsupported`) and from transport or server outcomes (`failed`), so a
missing commercial permission never degrades consumption. Authentication errors
are propagated rather than recorded as an availability status.

## Privacy boundary

Raw account numbers, property identifiers, supply-point identifiers, meter
serial numbers, device identifiers, and register identifiers remain in typed
runtime data only where provider calls require them. Home Assistant device
identifiers use an installation-local HMAC and device names use neutral ordinal
labels. Raw provider identifiers are not written to states, attributes, logs,
or diagnostics.

Run the fixed local probes described in
[`FIXTURE_REDACTION.md`](FIXTURE_REDACTION.md) before changing any GraphQL
contract.
