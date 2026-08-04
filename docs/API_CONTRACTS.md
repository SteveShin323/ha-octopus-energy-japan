# API contracts

Provider behaviour that shaped the code and is not obvious from reading it. Each item
was measured against a real account, not taken from documentation. The detailed
reasoning for a given query lives in a comment beside that query; this page is the index
a contributor should read before changing a GraphQL document.

- GraphQL endpoint: `https://api.oejp-kraken.energy/v1/graphql/`
- Auth server: `https://auth.oejp-kraken.energy`
- Official documentation: `https://docs.oejp-kraken.energy/graphql/guides/`

## Request limits

Published in the provider's API guide, and the reason for the constants the code uses. A
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
Reading it as one produces a false map of what an account may access.

## Readings

**One response is capped at 1488 intervals and truncates silently.** That is 31 days of
half-hourly data. Beyond it the oldest rows are dropped with no error and no flag, so an
over-wide window looks like missing history. Requests use seven-day windows. There is no
retention limit: every interval since supply started remains retrievable.

**A reading's `version` marks the billing lifecycle.** Intervals are reissued with a new
version when a period closes, and values change. This is why the ledger stores keyed
intervals rather than a running total, and why statistics are external rather than
recorder-backed.

**A billing period ends at the meter read, not at midnight.** Calendar projections use
Asia/Tokyo boundaries and will not equal an invoiced period. That is a presentation
choice, not a defect.

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
| An affordance for a mutation this integration does not perform | anything describing an action available in the provider's own app |
| Already published under another name | account-level payments duplicate the ledger transaction that is published |
| Measured to be unmaintained | the two "next reading date" fields were both in the past and disagreed with the reading day on the same supply point |
| Undecidable on the account available | four balance fields agreed, but all were zero; the account's only ledger reports `affectsAccountBalance: true`, so its balance is a component of the account balance already published |
| Not worth an entity's cost | a signed URL that would exceed the state length limit; fields that were null, zero, or equal to one already published |

The zero-balance row is worth reading twice: the first comparison reported four fields
"identical" and would have been written up as redundancy, when they were identical only
because every one of them was zero.

Publishing a field from any of these categories is a decision, not an omission to be
corrected. Revisit them individually.
