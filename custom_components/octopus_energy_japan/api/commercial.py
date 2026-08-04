"""Optional account, agreement, product, bill, and transaction operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from .auth import AuthenticatedGraphQLClient
from .client import GraphQLResult
from .errors import (
    GraphQLErrorDetail,
    OejpAuthenticationError,
    OejpAuthorizationError,
    OejpInvalidResponseError,
    OejpQueryValidationError,
    classify_graphql_error_details,
)

ACCOUNT_OVERVIEW_QUERY = """
query AccountCommercialOverview($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    number
    status
    balance
    overdueBalance
    hasActiveAgreement
    hasFutureAgreement
  }
}
"""

# `product.rates` is deliberately not requested. An account user is not authorised to
# read it: on 2026-08-04 asking for it returned `AUTHORIZATION/KT-CT-1111` at
# `account.marketSupplyAgreements.edges.0.node.product.rates`, and because GraphQL
# propagates that error to the nearest nullable parent, the whole `product` came back
# null — so the current product name was lost to fetch a field this integration never
# publishes. Removing it resolves the product and removes the error entirely.
ACCOUNT_AGREEMENTS_QUERY = """
query AccountCommercialAgreements($accountNumber: String!, $after: String) {
  account(accountNumber: $accountNumber) {
    number
    marketSupplyAgreements(first: 99, after: $after) {
      edges {
        node {
          id
          validFrom
          validTo
          agreedAt
          terminatedAt
          isActive
          product {
            id
            code
            displayName
            fullName
            marketName
          }
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

ACCOUNT_BILLING_QUERY = """
query AccountCommercialBilling($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    number
    bills(
      first: 1
      includeBillsWithoutPDF: true
      includeInvoices: true
      orderBy: FROM_DATE_DESC
    ) {
      edges {
        node {
          __typename
          id
          billType
          fromDate
          toDate
          issuedDate
          ... on StatementType {
            paymentDueDate
            statementTotalCharges: totalCharges { grossTotal }
            status
          }
          ... on PeriodBasedDocumentType {
            periodTotalCharges: totalCharges { grossTotal }
            periodIsAnnulled: isAnnulled
            periodIsHeld: isHeld
          }
          ... on InvoiceType {
            invoiceGrossAmount: grossAmount
            invoiceIsAnnulled: isAnnulled
            invoiceIsHeld: isHeld
          }
        }
      }
    }
    ledgers {
      transactions(first: 1, orderBy: POSTED_DATE_DESC) {
        edges {
          node {
            __typename
            id
            postedDate
            createdAt
            amount
            isHeld
            isIssued
            isReversed
            title
            reasonCode
          }
        }
      }
    }
  }
}
"""
# Transactions are read from the ledger, not from `account.transactions`. Measured against
# a real account on 2026-08-04: `account.transactions` returned an empty connection while
# `ledgers[].transactions` returned three — a payment, a charge, and a credit, with posted
# dates. The latest-transaction sensor was therefore permanently empty for accounts whose
# activity lives on the ledger, which appears to be the normal arrangement.


class CommercialFeature(StrEnum):
    """Independently optional commercial operation families."""

    OVERVIEW = "overview"
    AGREEMENTS = "agreements"
    BILLING = "billing"


class CommercialAvailability(StrEnum):
    """Safe availability state for an optional operation."""

    AVAILABLE = "available"
    PARTIAL = "partial"
    FORBIDDEN = "forbidden"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CommercialAccess:
    """Privacy-safe result status for one optional operation."""

    feature: CommercialFeature
    availability: CommercialAvailability
    error_codes: tuple[str, ...] = ()
    error_types: tuple[str, ...] = ()
    error_paths: tuple[tuple[str | int, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class AccountOverview:
    """Account status and provider-denominated minor-unit balances."""

    account_id: str
    status: str | None
    balance_minor: int | None
    overdue_balance_minor: int | None
    has_active_agreement: bool | None
    has_future_agreement: bool | None


@dataclass(frozen=True, slots=True)
class AgreementPage:
    """One validated agreement connection page."""

    agreements: tuple[AgreementSummary, ...]
    has_next_page: bool
    end_cursor: str | None


@dataclass(frozen=True, slots=True)
class ProductSummary:
    """Supply product attached to an agreement."""

    id: str
    code: str | None
    display_name: str | None
    full_name: str | None
    market_name: str | None


@dataclass(frozen=True, slots=True)
class AgreementSummary:
    """One market-supply agreement and its product."""

    id: str
    valid_from: datetime
    valid_to: datetime | None
    agreed_at: datetime | None
    terminated_at: datetime | None
    is_active: bool | None
    product: ProductSummary | None


@dataclass(frozen=True, slots=True)
class BillSummary:
    """Latest bill metadata and provider minor-unit gross charge."""

    id: str
    type_name: str
    bill_type: str | None
    from_date: date | None
    to_date: date | None
    issued_date: date | None
    due_date: date | None
    gross_amount_minor: int | None
    status: str | None
    is_annulled: bool | None
    is_held: bool | None


@dataclass(frozen=True, slots=True)
class TransactionSummary:
    """Latest posted account transaction without provider-rendered detail."""

    id: str
    type_name: str
    posted_date: date | None
    created_at: datetime | None
    amount_minor: int | None
    is_held: bool | None
    is_issued: bool | None
    is_reversed: bool
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class AccountCommercialSnapshot:
    """Typed optional commercial data for one account."""

    account_id: str
    overview: AccountOverview | None = None
    agreements: tuple[AgreementSummary, ...] = ()
    latest_bill: BillSummary | None = None
    latest_transaction: TransactionSummary | None = None
    access: tuple[CommercialAccess, ...] = ()
    observed_at: datetime | None = None

    def feature_access(self, feature: CommercialFeature) -> CommercialAccess:
        """Return one feature status, defaulting to a safe failed state."""
        return next(
            (status for status in self.access if status.feature is feature),
            CommercialAccess(feature, CommercialAvailability.FAILED),
        )

    def current_agreement(self, at: datetime) -> AgreementSummary | None:
        """Return the active agreement at a timezone-aware instant."""
        instant = _utc(at)
        candidates = tuple(
            agreement
            for agreement in self.agreements
            if agreement.valid_from <= instant
            and (agreement.valid_to is None or instant < agreement.valid_to)
            and agreement.terminated_at is None
        )
        if not candidates:
            return None
        return max(candidates, key=lambda agreement: agreement.valid_from)


async def async_fetch_account_commercial_snapshot(
    client: AuthenticatedGraphQLClient,
    account_id: str,
    *,
    observed_at: datetime,
) -> AccountCommercialSnapshot:
    """Fetch independent optional commercial operations for one account."""
    if not account_id:
        raise ValueError("account_id must not be empty")
    observed = _utc(observed_at)
    overview_result = await client.execute_optional(
        ACCOUNT_OVERVIEW_QUERY,
        {"accountNumber": account_id},
    )
    overview_access = _access(CommercialFeature.OVERVIEW, overview_result)
    agreements, agreements_access = await _async_fetch_agreements(client, account_id)
    billing_result = await client.execute_optional(
        ACCOUNT_BILLING_QUERY,
        {"accountNumber": account_id},
    )
    billing_access = _access(CommercialFeature.BILLING, billing_result)
    overview = (
        parse_account_overview(overview_result.data, account_id)
        if overview_result.data is not None
        else None
    )
    bill, transaction = (
        parse_account_billing(billing_result.data, account_id)
        if billing_result.data is not None
        else (None, None)
    )
    return AccountCommercialSnapshot(
        account_id,
        overview,
        agreements,
        bill,
        transaction,
        (overview_access, agreements_access, billing_access),
        observed,
    )


def parse_account_overview(
    data: Mapping[str, Any],
    expected_account_id: str,
) -> AccountOverview:
    """Parse a strict account overview from optional-operation data."""
    account = _account(data, expected_account_id, "overview")
    return AccountOverview(
        expected_account_id,
        _optional_string(account.get("status")),
        _optional_int(account.get("balance"), "Account balance"),
        _optional_int(account.get("overdueBalance"), "Account overdue balance"),
        _optional_bool(account.get("hasActiveAgreement"), "hasActiveAgreement"),
        _optional_bool(account.get("hasFutureAgreement"), "hasFutureAgreement"),
    )


def parse_account_agreements(
    data: Mapping[str, Any],
    expected_account_id: str,
) -> tuple[AgreementSummary, ...]:
    """Parse deterministic agreement and product summaries."""
    page = parse_account_agreements_page(data, expected_account_id)
    if page.has_next_page:
        raise OejpInvalidResponseError("Agreement response contained another page")
    return page.agreements


def parse_account_agreements_page(
    data: Mapping[str, Any],
    expected_account_id: str,
) -> AgreementPage:
    """Parse one deterministic agreement connection page."""
    account = _account(data, expected_account_id, "agreements")
    connection = _required_mapping(
        account.get("marketSupplyAgreements"),
        "Agreement response was missing marketSupplyAgreements",
    )
    page_info = _required_mapping(
        connection.get("pageInfo"),
        "Agreement response was missing pageInfo",
    )
    has_next = _required_bool(page_info.get("hasNextPage"), "Agreement hasNextPage")
    end_cursor = _optional_string(page_info.get("endCursor"))
    if has_next and end_cursor is None:
        raise OejpInvalidResponseError("Agreement hasNextPage was true without endCursor")
    edges = _required_list(connection.get("edges"), "Agreement response was missing edges")
    by_id: dict[str, AgreementSummary] = {}
    for edge_value in edges:
        edge = _required_mapping(edge_value, "Agreement response contained a malformed edge")
        node = _required_mapping(edge.get("node"), "Agreement response edge was missing node")
        agreement = _parse_agreement(node)
        existing = by_id.get(agreement.id)
        if existing is not None and existing != agreement:
            raise OejpInvalidResponseError("Agreement response contained a conflicting duplicate")
        by_id[agreement.id] = agreement
    return AgreementPage(
        tuple(sorted(by_id.values(), key=lambda value: (value.valid_from, value.id))),
        has_next,
        end_cursor,
    )


async def _async_fetch_agreements(
    client: AuthenticatedGraphQLClient,
    account_id: str,
) -> tuple[tuple[AgreementSummary, ...], CommercialAccess]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    agreements: dict[str, AgreementSummary] = {}
    for _page_number in range(1_000):
        result = await client.execute_optional(
            ACCOUNT_AGREEMENTS_QUERY,
            {"accountNumber": account_id, "after": cursor},
        )
        access = _access(CommercialFeature.AGREEMENTS, result)
        if result.data is None:
            return (), access
        page = parse_account_agreements_page(result.data, account_id)
        for agreement in page.agreements:
            existing = agreements.get(agreement.id)
            if existing is not None and existing != agreement:
                raise OejpInvalidResponseError(
                    "Agreement pagination contained a conflicting duplicate"
                )
            agreements[agreement.id] = agreement
        if access.availability is CommercialAvailability.PARTIAL or not page.has_next_page:
            return (
                tuple(sorted(agreements.values(), key=lambda value: (value.valid_from, value.id))),
                access,
            )
        if page.end_cursor is None or page.end_cursor in seen_cursors:
            raise OejpInvalidResponseError("Agreement pagination cursor repeated")
        seen_cursors.add(page.end_cursor)
        cursor = page.end_cursor
    raise OejpInvalidResponseError("Agreement pagination exceeded the page safety limit")


def parse_account_billing(
    data: Mapping[str, Any],
    expected_account_id: str,
) -> tuple[BillSummary | None, TransactionSummary | None]:
    """Parse only the newest bill and transaction summaries."""
    account = _account(data, expected_account_id, "billing")
    bills = _required_mapping(account.get("bills"), "Billing response was missing bills")
    bill_nodes = _connection_nodes(bills, "Bill")
    if len(bill_nodes) > 1:
        raise OejpInvalidResponseError("Billing response exceeded the requested result limit")
    return (
        _parse_bill(bill_nodes[0]) if bill_nodes else None,
        _latest_ledger_transaction(account),
    )


def _latest_ledger_transaction(account: Mapping[str, Any]) -> TransactionSummary | None:
    """Return the newest transaction across the account's ledgers.

    An account may hold more than one ledger, and each is asked for its own newest
    transaction, so the newest overall is chosen here. A ledger with no posted date sorts
    last rather than being dropped: it is still a transaction the customer can see.

    A nulled `ledgers` is tolerated rather than raised on. That is the shape of a partial
    response, which the billing access record already reports, and refusing it would
    discard the bill that arrived in the same response over a secondary field.
    """
    ledgers = account.get("ledgers")
    if ledgers is None:
        return None
    latest: TransactionSummary | None = None
    for value in _required_list(ledgers, "Billing response returned malformed ledgers"):
        ledger = _required_mapping(value, "Billing response contained a malformed ledger")
        transactions = ledger.get("transactions")
        if transactions is None:
            continue
        nodes = _connection_nodes(
            _required_mapping(transactions, "Billing response returned malformed transactions"),
            "Transaction",
        )
        if len(nodes) > 1:
            raise OejpInvalidResponseError("Billing response exceeded the requested result limit")
        if not nodes:
            continue
        candidate = _parse_transaction(nodes[0])
        if latest is None or _transaction_order(candidate) > _transaction_order(latest):
            latest = candidate
    return latest


def _transaction_order(transaction: TransactionSummary) -> tuple[date, datetime]:
    """Sort key placing a transaction with no dates behind any that has them."""
    return (
        transaction.posted_date or date.min,
        transaction.created_at or datetime.min.replace(tzinfo=UTC),
    )


def _parse_agreement(value: Mapping[str, Any]) -> AgreementSummary:
    product_value = value.get("product")
    product = (
        _parse_product(_required_mapping(product_value, "Agreement product was malformed"))
        if product_value is not None
        else None
    )
    return AgreementSummary(
        id=_required_identifier(value, "id", "Agreement"),
        valid_from=_required_datetime(value.get("validFrom"), "Agreement validFrom"),
        valid_to=_optional_datetime(value.get("validTo"), "Agreement validTo"),
        agreed_at=_optional_datetime(value.get("agreedAt"), "Agreement agreedAt"),
        terminated_at=_optional_datetime(value.get("terminatedAt"), "Agreement terminatedAt"),
        is_active=_optional_bool(value.get("isActive"), "Agreement isActive"),
        product=product,
    )


def _parse_product(value: Mapping[str, Any]) -> ProductSummary:
    return ProductSummary(
        id=_required_identifier(value, "id", "Product"),
        code=_optional_string(value.get("code")),
        display_name=_optional_string(value.get("displayName")),
        full_name=_optional_string(value.get("fullName")),
        market_name=_optional_string(value.get("marketName")),
    )


def _parse_bill(value: Mapping[str, Any]) -> BillSummary:
    # `isHeld`, `isAnnulled`, `totalCharges`, and `grossAmount` are aliased per
    # inline fragment because the bill implementations disagree on nullability and
    # on the total type, which GraphQL rejects for one shared response name.
    gross_total = _first_gross_total(value)
    gross_amount = _optional_int(value.get("invoiceGrossAmount"), "Bill grossAmount")
    if gross_total is not None and gross_amount is not None and gross_total != gross_amount:
        raise OejpInvalidResponseError("Bill response contained conflicting gross amounts")
    return BillSummary(
        id=_required_identifier(value, "id", "Bill"),
        type_name=_required_identifier(value, "__typename", "Bill"),
        bill_type=_optional_string(value.get("billType")),
        from_date=_optional_date(value.get("fromDate"), "Bill fromDate"),
        to_date=_optional_date(value.get("toDate"), "Bill toDate"),
        issued_date=_optional_date(value.get("issuedDate"), "Bill issuedDate"),
        due_date=_optional_date(value.get("paymentDueDate"), "Bill paymentDueDate"),
        gross_amount_minor=gross_total if gross_total is not None else gross_amount,
        status=_optional_string(value.get("status")),
        is_annulled=_first_bool(value, ("periodIsAnnulled", "invoiceIsAnnulled"), "isAnnulled"),
        is_held=_first_bool(value, ("periodIsHeld", "invoiceIsHeld"), "isHeld"),
    )


def _first_gross_total(value: Mapping[str, Any]) -> int | None:
    for key in ("statementTotalCharges", "periodTotalCharges"):
        total = value.get(key)
        if total is None:
            continue
        return _optional_int(
            _required_mapping(total, "Bill totalCharges was malformed").get("grossTotal"),
            "Bill grossTotal",
        )
    return None


def _first_bool(
    value: Mapping[str, Any],
    keys: tuple[str, ...],
    context: str,
) -> bool | None:
    for key in keys:
        if (found := value.get(key)) is not None:
            return _required_bool(found, f"Bill {context}")
    return None


def _parse_transaction(value: Mapping[str, Any]) -> TransactionSummary:
    reversed_value = value.get("isReversed")
    return TransactionSummary(
        id=_required_identifier(value, "id", "Transaction"),
        type_name=_required_identifier(value, "__typename", "Transaction"),
        posted_date=_optional_date(value.get("postedDate"), "Transaction postedDate"),
        created_at=_optional_datetime(value.get("createdAt"), "Transaction createdAt"),
        amount_minor=_optional_int(value.get("amount"), "Transaction amount"),
        is_held=_optional_bool(value.get("isHeld"), "Transaction isHeld"),
        is_issued=_optional_bool(value.get("isIssued"), "Transaction isIssued"),
        is_reversed=(
            _required_bool(reversed_value, "Transaction isReversed")
            if reversed_value is not None
            else False
        ),
        reason_code=_optional_string(value.get("reasonCode")),
    )


def _access(feature: CommercialFeature, result: GraphQLResult) -> CommercialAccess:
    if not result.errors:
        return CommercialAccess(feature, CommercialAvailability.AVAILABLE)
    error = classify_graphql_error_details(result.errors, retry_after=result.retry_after)
    if isinstance(error, OejpAuthenticationError):
        raise error
    if result.data is not None:
        availability = CommercialAvailability.PARTIAL
    elif isinstance(error, OejpAuthorizationError):
        availability = CommercialAvailability.FORBIDDEN
    elif isinstance(error, OejpQueryValidationError):
        availability = CommercialAvailability.UNSUPPORTED
    else:
        availability = CommercialAvailability.FAILED
    return CommercialAccess(
        feature,
        availability,
        _safe_values(result.errors, "error_code"),
        _safe_values(result.errors, "error_type"),
        tuple(detail.path for detail in result.errors if detail.path),
    )


def _safe_values(
    errors: tuple[GraphQLErrorDetail, ...],
    attribute: str,
) -> tuple[str, ...]:
    values = {
        value
        for detail in errors
        if isinstance((value := getattr(detail, attribute)), str) and value
    }
    return tuple(sorted(values))


def _account(
    data: Mapping[str, Any],
    expected_account_id: str,
    context: str,
) -> Mapping[str, Any]:
    account = _required_mapping(
        data.get("account"),
        f"Commercial {context} response was missing account",
    )
    returned = _required_identifier(account, "number", "Commercial account")
    if returned != expected_account_id:
        raise OejpInvalidResponseError("Commercial response returned a different account")
    return account


def _connection_nodes(value: Mapping[str, Any], context: str) -> tuple[Mapping[str, Any], ...]:
    edges = _required_list(value.get("edges"), f"{context} response was missing edges")
    nodes: list[Mapping[str, Any]] = []
    for edge_value in edges:
        edge = _required_mapping(edge_value, f"{context} response contained a malformed edge")
        nodes.append(_required_mapping(edge.get("node"), f"{context} edge was missing node"))
    return tuple(nodes)


def _required_mapping(value: object, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OejpInvalidResponseError(message)
    return value


def _required_list(value: object, message: str) -> list[Any]:
    if not isinstance(value, list):
        raise OejpInvalidResponseError(message)
    return value


def _required_identifier(value: Mapping[str, Any], key: str, context: str) -> str:
    identifier = _optional_string(value.get(key))
    if identifier is None:
        raw = value.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return str(raw)
        raise OejpInvalidResponseError(f"{context} was missing {key}")
    return identifier


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_int(value: object, context: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise OejpInvalidResponseError(f"{context} was malformed")
    return value


def _required_bool(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise OejpInvalidResponseError(f"{context} was malformed")
    return value


def _optional_bool(value: object, context: str) -> bool | None:
    if value is None:
        return None
    return _required_bool(value, context)


def _required_datetime(value: object, context: str) -> datetime:
    parsed = _optional_datetime(value, context)
    if parsed is None:
        raise OejpInvalidResponseError(f"{context} was missing")
    return parsed


def _optional_datetime(value: object, context: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OejpInvalidResponseError(f"{context} was malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise OejpInvalidResponseError(f"{context} was malformed") from err
    if parsed.tzinfo is None:
        raise OejpInvalidResponseError(f"{context} was not timezone-aware")
    return parsed.astimezone(UTC)


def _optional_date(value: object, context: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OejpInvalidResponseError(f"{context} was malformed")
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise OejpInvalidResponseError(f"{context} was malformed") from err


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Commercial timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "ACCOUNT_AGREEMENTS_QUERY",
    "ACCOUNT_BILLING_QUERY",
    "ACCOUNT_OVERVIEW_QUERY",
    "AccountCommercialSnapshot",
    "AccountOverview",
    "AgreementPage",
    "AgreementSummary",
    "BillSummary",
    "CommercialAccess",
    "CommercialAvailability",
    "CommercialFeature",
    "ProductSummary",
    "TransactionSummary",
    "async_fetch_account_commercial_snapshot",
    "parse_account_agreements",
    "parse_account_agreements_page",
    "parse_account_billing",
    "parse_account_overview",
]
