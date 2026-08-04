"""Tests for optional account, product, bill, and transaction contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from custom_components.octopus_energy_japan.api import (
    ACCOUNT_AGREEMENTS_QUERY,
    ACCOUNT_BILLING_QUERY,
    ACCOUNT_OVERVIEW_QUERY,
    AccountCommercialSnapshot,
    AuthenticatedGraphQLClient,
    CommercialAvailability,
    CommercialFeature,
    GraphQLErrorDetail,
    GraphQLResult,
    OejpAuthenticationError,
    OejpInvalidResponseError,
    async_fetch_account_commercial_snapshot,
    parse_account_agreements,
    parse_account_billing,
    parse_account_overview,
)

ACCOUNT = "PRIVATE-ACCOUNT"
NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _overview() -> dict[str, object]:
    return {
        "account": {
            "number": ACCOUNT,
            "status": "ACTIVE",
            "balance": 1234,
            "overdueBalance": 50,
            "hasActiveAgreement": True,
            "hasFutureAgreement": False,
        }
    }


def _agreements() -> dict[str, object]:
    return {
        "account": {
            "number": ACCOUNT,
            "marketSupplyAgreements": {
                "edges": [
                    {
                        "node": {
                            "id": "agreement-2",
                            "validFrom": "2026-07-01T00:00:00+09:00",
                            "validTo": None,
                            "agreedAt": "2026-06-01T01:00:00Z",
                            "terminatedAt": None,
                            "isActive": True,
                            "product": {
                                "id": "product-2",
                                "code": "OEJP-2",
                                "displayName": "Octopus plan",
                                "fullName": "Octopus Energy Japan plan",
                                "marketName": "JPN_ELECTRICITY",
                            },
                        }
                    },
                    {
                        "node": {
                            "id": 1,
                            "validFrom": "2025-07-01T00:00:00+09:00",
                            "validTo": "2026-07-01T00:00:00+09:00",
                            "agreedAt": None,
                            "terminatedAt": None,
                            "isActive": False,
                            "product": None,
                        }
                    },
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": "cursor"},
            },
        }
    }


def _billing() -> dict[str, object]:
    return {
        "account": {
            "number": ACCOUNT,
            "bills": {
                "edges": [
                    {
                        "node": {
                            "__typename": "StatementType",
                            "id": "bill-1",
                            "billType": "STATEMENT",
                            "fromDate": "2026-06-01",
                            "toDate": "2026-06-30",
                            "issuedDate": "2026-07-05",
                            "paymentDueDate": "2026-07-31",
                            "statementTotalCharges": {"grossTotal": 8765},
                            "status": "CLOSED",
                        }
                    }
                ]
            },
            "ledgers": [
                {
                    "transactions": {
                        "edges": [
                            {
                                "node": {
                                    "__typename": "Payment",
                                    "id": "transaction-1",
                                    "postedDate": "2026-07-10",
                                    "createdAt": "2026-07-10T01:02:03Z",
                                    "amount": -8765,
                                    "isHeld": False,
                                    "isIssued": True,
                                    "isReversed": False,
                                    "title": "provider text is intentionally ignored",
                                    "reasonCode": "PAYMENT",
                                }
                            }
                        ]
                    }
                }
            ],
        }
    }


def test_parsers_return_typed_deterministic_commercial_data() -> None:
    overview = parse_account_overview(_overview(), ACCOUNT)
    agreements = parse_account_agreements(_agreements(), ACCOUNT)
    bill, transaction = parse_account_billing(_billing(), ACCOUNT)

    assert overview.balance_minor == 1234
    assert overview.overdue_balance_minor == 50
    assert [agreement.id for agreement in agreements] == ["1", "agreement-2"]
    assert agreements[-1].valid_from == datetime(2026, 6, 30, 15, tzinfo=UTC)
    assert agreements[-1].product is not None
    assert bill is not None
    assert bill.gross_amount_minor == 8765
    assert bill.due_date is not None and bill.due_date.isoformat() == "2026-07-31"
    assert transaction is not None
    assert transaction.amount_minor == -8765
    assert transaction.created_at == datetime(2026, 7, 10, 1, 2, 3, tzinfo=UTC)


def test_the_agreements_query_does_not_request_rates() -> None:
    """An account user may not read `product.rates`, and asking costs the product.

    On 2026-08-04 requesting it returned `AUTHORIZATION/KT-CT-1111` at
    `account.marketSupplyAgreements.edges.0.node.product.rates`. GraphQL propagates that
    error to the nearest nullable parent, so the entire `product` came back null and the
    current product name was lost — to fetch a field this integration never publishes.
    """
    assert "rates" not in ACCOUNT_AGREEMENTS_QUERY
    assert "pricePerUnit" not in ACCOUNT_AGREEMENTS_QUERY
    # The fields that survive are the ones actually published.
    for field in ("id", "code", "displayName", "fullName", "marketName"):
        assert field in ACCOUNT_AGREEMENTS_QUERY


def test_a_product_without_rates_still_parses() -> None:
    agreements = parse_account_agreements(_agreements(), ACCOUNT)

    product = agreements[-1].product
    assert product is not None
    assert product.display_name == "Octopus plan"
    assert not hasattr(product, "rates")


def test_absent_optional_agreement_flags_fall_back_to_safe_defaults() -> None:
    payload = deepcopy(_agreements())
    node = payload["account"]["marketSupplyAgreements"]["edges"][0]["node"]  # type: ignore[index]
    node["isActive"] = None

    agreements = parse_account_agreements(payload, ACCOUNT)

    assert agreements[-1].is_active is None


def test_period_based_document_held_and_annulled_aliases_are_read() -> None:
    payload = deepcopy(_billing())
    node = payload["account"]["bills"]["edges"][0]["node"]  # type: ignore[index]
    node.pop("statementTotalCharges")
    node.pop("paymentDueDate")
    node.update(
        {
            "__typename": "PeriodBasedDocumentType",
            "periodTotalCharges": {"grossTotal": 5000},
            "periodIsAnnulled": False,
            "periodIsHeld": True,
        }
    )

    bill, _ = parse_account_billing(payload, ACCOUNT)

    assert bill is not None
    assert bill.type_name == "PeriodBasedDocumentType"
    assert bill.gross_amount_minor == 5000
    assert bill.is_annulled is False
    assert bill.is_held is True
    assert bill.due_date is None


def test_current_agreement_uses_half_open_utc_periods() -> None:
    snapshot = AccountCommercialSnapshot(
        ACCOUNT, agreements=parse_account_agreements(_agreements(), ACCOUNT)
    )

    assert snapshot.current_agreement(datetime(2026, 6, 30, 14, 59, tzinfo=UTC)).id == "1"  # type: ignore[union-attr]
    assert snapshot.current_agreement(datetime(2026, 6, 30, 15, tzinfo=UTC)).id == "agreement-2"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="timezone-aware"):
        snapshot.current_agreement(datetime(2026, 8, 3))  # noqa: DTZ001


async def test_fetch_executes_separate_optional_operations_and_preserves_partial_status() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute_optional.side_effect = [
        GraphQLResult(_overview()),
        GraphQLResult(
            _agreements(),
            (
                GraphQLErrorDetail(
                    "safe",
                    error_type="AUTHORIZATION",
                    error_code="KT-CT-4177",
                    path=("account", "marketSupplyAgreements", "optionalField"),
                ),
            ),
        ),
        GraphQLResult(None, (GraphQLErrorDetail("safe", error_type="AUTHORIZATION"),)),
    ]

    snapshot = await async_fetch_account_commercial_snapshot(client, ACCOUNT, observed_at=NOW)

    assert snapshot.overview is not None
    assert len(snapshot.agreements) == 2
    assert snapshot.latest_bill is None
    assert (
        snapshot.feature_access(CommercialFeature.OVERVIEW).availability
        is CommercialAvailability.AVAILABLE
    )
    assert (
        snapshot.feature_access(CommercialFeature.AGREEMENTS).availability
        is CommercialAvailability.PARTIAL
    )
    billing_access = snapshot.feature_access(CommercialFeature.BILLING)
    assert billing_access.availability is CommercialAvailability.FORBIDDEN
    assert billing_access.error_types == ("AUTHORIZATION",)
    assert [call.args for call in client.execute_optional.await_args_list] == [
        (ACCOUNT_OVERVIEW_QUERY, {"accountNumber": ACCOUNT}),
        (ACCOUNT_AGREEMENTS_QUERY, {"accountNumber": ACCOUNT, "after": None}),
        (ACCOUNT_BILLING_QUERY, {"accountNumber": ACCOUNT}),
    ]


async def test_fetch_raises_terminal_authentication_instead_of_hiding_it() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute_optional.side_effect = [
        GraphQLResult(
            None,
            (GraphQLErrorDetail("safe", error_type="AUTHENTICATION"),),
        )
    ]

    with pytest.raises(OejpAuthenticationError):
        await async_fetch_account_commercial_snapshot(client, ACCOUNT, observed_at=NOW)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.clear(),
        lambda value: value["account"].update({"number": "DIFFERENT"}),
        lambda value: value["account"].update({"balance": True}),
        lambda value: value["account"].update({"hasActiveAgreement": "yes"}),
    ],
)
def test_overview_rejects_malformed_or_mismatched_data(mutation: object) -> None:
    payload = _overview()
    mutation(payload)  # type: ignore[operator]
    with pytest.raises(OejpInvalidResponseError):
        parse_account_overview(payload, ACCOUNT)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["account"]["marketSupplyAgreements"]["pageInfo"].update(
            {"hasNextPage": True}
        ),
        lambda value: value["account"]["marketSupplyAgreements"].update({"edges": None}),
        lambda value: value["account"]["marketSupplyAgreements"]["edges"][0]["node"].update(
            {"validFrom": "invalid"}
        ),
    ],
)
def test_agreements_reject_malformed_contract_data(mutation: object) -> None:
    payload = deepcopy(_agreements())
    mutation(payload)  # type: ignore[operator]
    with pytest.raises(OejpInvalidResponseError):
        parse_account_agreements(payload, ACCOUNT)


async def test_fetch_rejects_an_empty_account_identifier() -> None:
    with pytest.raises(ValueError, match="account_id"):
        await async_fetch_account_commercial_snapshot(
            AsyncMock(spec=AuthenticatedGraphQLClient),
            "",
            observed_at=NOW,
        )


def _page(agreement_id: str, *, has_next: bool, cursor: str | None) -> dict[str, object]:
    return {
        "account": {
            "number": ACCOUNT,
            "marketSupplyAgreements": {
                "edges": [
                    {
                        "node": {
                            "id": agreement_id,
                            "validFrom": "2026-07-01T00:00:00+09:00",
                            "validTo": None,
                            "agreedAt": None,
                            "terminatedAt": None,
                            "isActive": True,
                            "product": None,
                        }
                    }
                ],
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            },
        }
    }


async def test_agreement_pagination_follows_every_cursor_exactly_once() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute_optional.side_effect = [
        GraphQLResult(_overview()),
        GraphQLResult(_page("first", has_next=True, cursor="cursor-1")),
        GraphQLResult(_page("second", has_next=False, cursor="cursor-2")),
        GraphQLResult(_billing()),
    ]

    snapshot = await async_fetch_account_commercial_snapshot(client, ACCOUNT, observed_at=NOW)

    assert [agreement.id for agreement in snapshot.agreements] == ["first", "second"]
    assert [call.args[1].get("after") for call in client.execute_optional.await_args_list[1:3]] == [
        None,
        "cursor-1",
    ]


async def test_agreement_pagination_rejects_a_repeated_cursor() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute_optional.side_effect = [
        GraphQLResult(_overview()),
        GraphQLResult(_page("first", has_next=True, cursor="repeat")),
        GraphQLResult(_page("second", has_next=True, cursor="repeat")),
    ]

    with pytest.raises(OejpInvalidResponseError, match="cursor repeated"):
        await async_fetch_account_commercial_snapshot(client, ACCOUNT, observed_at=NOW)


async def test_agreement_pagination_stops_at_the_page_safety_limit() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    pages = iter(range(2_000))

    def _next_page(query: str, _variables: dict[str, object]) -> GraphQLResult:
        if query == ACCOUNT_OVERVIEW_QUERY:
            return GraphQLResult(_overview())
        index = next(pages)
        return GraphQLResult(_page(f"agreement-{index}", has_next=True, cursor=f"cursor-{index}"))

    client.execute_optional.side_effect = _next_page

    with pytest.raises(OejpInvalidResponseError, match="page safety limit"):
        await async_fetch_account_commercial_snapshot(client, ACCOUNT, observed_at=NOW)


async def test_agreement_pagination_reports_conflicting_duplicates_across_pages() -> None:
    conflicting = _page("same", has_next=False, cursor=None)
    conflicting["account"]["marketSupplyAgreements"]["edges"][0]["node"].update(  # type: ignore[index]
        {"isActive": False}
    )
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute_optional.side_effect = [
        GraphQLResult(_overview()),
        GraphQLResult(_page("same", has_next=True, cursor="cursor-1")),
        GraphQLResult(conflicting),
    ]

    with pytest.raises(OejpInvalidResponseError, match="pagination contained a conflicting"):
        await async_fetch_account_commercial_snapshot(client, ACCOUNT, observed_at=NOW)


async def test_agreements_report_no_values_when_the_operation_returns_no_data() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute_optional.side_effect = [
        GraphQLResult(_overview()),
        GraphQLResult(None, (GraphQLErrorDetail("safe", error_type="VALIDATION"),)),
        GraphQLResult(_billing()),
    ]

    snapshot = await async_fetch_account_commercial_snapshot(client, ACCOUNT, observed_at=NOW)

    assert snapshot.agreements == ()
    assert (
        snapshot.feature_access(CommercialFeature.AGREEMENTS).availability
        is CommercialAvailability.UNSUPPORTED
    )


async def test_unclassified_optional_failure_is_reported_as_failed() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute_optional.side_effect = [
        GraphQLResult(None, (GraphQLErrorDetail("safe", error_type="INTERNAL"),)),
        GraphQLResult(_agreements()),
        GraphQLResult(_billing()),
    ]

    snapshot = await async_fetch_account_commercial_snapshot(client, ACCOUNT, observed_at=NOW)

    assert snapshot.overview is None
    assert (
        snapshot.feature_access(CommercialFeature.OVERVIEW).availability
        is CommercialAvailability.FAILED
    )


def test_single_page_rejects_a_conflicting_duplicate_agreement() -> None:
    payload = deepcopy(_agreements())
    edges = payload["account"]["marketSupplyAgreements"]["edges"]  # type: ignore[index]
    duplicate = deepcopy(edges[0])
    duplicate["node"]["isActive"] = False
    edges.append(duplicate)

    with pytest.raises(OejpInvalidResponseError, match="response contained a conflicting"):
        parse_account_agreements(payload, ACCOUNT)


def test_page_info_must_provide_a_cursor_when_more_pages_exist() -> None:
    payload = deepcopy(_agreements())
    payload["account"]["marketSupplyAgreements"]["pageInfo"].update(  # type: ignore[index]
        {"hasNextPage": True, "endCursor": None}
    )

    with pytest.raises(OejpInvalidResponseError, match="without endCursor"):
        parse_account_agreements(payload, ACCOUNT)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda node: node.pop("id"), "was missing id"),
        (lambda node: node.update({"validFrom": None}), "validFrom was missing"),
        (lambda node: node.update({"validTo": 123}), "validTo was malformed"),
        (
            lambda node: node.update({"validTo": "2026-07-01T00:00:00"}),
            "not timezone-aware",
        ),
    ],
)
def test_agreement_field_contracts_are_enforced(mutation: object, message: str) -> None:
    payload = deepcopy(_agreements())
    mutation(payload["account"]["marketSupplyAgreements"]["edges"][0]["node"])  # type: ignore[index,operator]

    with pytest.raises(OejpInvalidResponseError, match=message):
        parse_account_agreements(payload, ACCOUNT)


def test_invoice_gross_amount_is_used_when_no_charge_breakdown_exists() -> None:
    payload = deepcopy(_billing())
    node = payload["account"]["bills"]["edges"][0]["node"]  # type: ignore[index]
    node.pop("statementTotalCharges")
    node.update({"__typename": "InvoiceType", "invoiceGrossAmount": 4321})

    bill, _ = parse_account_billing(payload, ACCOUNT)

    assert bill is not None
    assert bill.type_name == "InvoiceType"
    assert bill.gross_amount_minor == 4321


@pytest.mark.parametrize("value", [123, "2026-13-40"])
def test_malformed_bill_dates_are_rejected(value: object) -> None:
    payload = deepcopy(_billing())
    payload["account"]["bills"]["edges"][0]["node"].update({"fromDate": value})  # type: ignore[index]

    with pytest.raises(OejpInvalidResponseError, match="fromDate was malformed"):
        parse_account_billing(payload, ACCOUNT)


def test_billing_rejects_conflicting_or_excess_results() -> None:
    conflicting = deepcopy(_billing())
    bill = conflicting["account"]["bills"]["edges"][0]["node"]  # type: ignore[index]
    bill["invoiceGrossAmount"] = 1
    with pytest.raises(OejpInvalidResponseError, match="conflicting gross"):
        parse_account_billing(conflicting, ACCOUNT)

    excess = deepcopy(_billing())
    ledger = excess["account"]["ledgers"][0]  # type: ignore[index]
    edges = ledger["transactions"]["edges"]
    edges.append(deepcopy(edges[0]))
    with pytest.raises(OejpInvalidResponseError, match="result limit"):
        parse_account_billing(excess, ACCOUNT)


def test_transactions_are_read_from_the_ledger_not_from_the_account() -> None:
    """The account-level connection is empty on a real account; the ledger's is not.

    Measured on 2026-08-04: `account.transactions` returned zero edges while
    `ledgers[].transactions` returned a payment, a charge and a credit. Reading the
    account-level field looks more direct and is what this integration shipped, which left
    the latest-transaction sensor permanently empty. This test fails if the query or the
    parser moves back, so the reason is not lost.
    """
    assert "ledgers {" in ACCOUNT_BILLING_QUERY
    # The account-level field must not be selected at all: a populated one would mask the
    # ledger read during development and hide the regression.
    account_level = ACCOUNT_BILLING_QUERY.split("ledgers {")[0]
    assert "transactions(" not in account_level

    decoy = deepcopy(_billing())
    decoy["account"]["transactions"] = decoy["account"].pop("ledgers")[0][  # type: ignore[index]
        "transactions"
    ]

    assert parse_account_billing(decoy, ACCOUNT)[1] is None


def test_the_newest_transaction_across_several_ledgers_wins() -> None:
    """An account may hold more than one ledger, each answering with its own newest."""
    payload = deepcopy(_billing())
    ledgers = payload["account"]["ledgers"]  # type: ignore[index]
    older = deepcopy(ledgers[0])
    older["transactions"]["edges"][0]["node"] |= {
        "id": "transaction-0",
        "postedDate": "2026-05-01",
        "createdAt": "2026-05-01T00:00:00Z",
    }
    # Newest second in one ordering and first in the other, so neither list position wins.
    for arrangement in ([older, ledgers[0]], [ledgers[0], older]):
        payload["account"]["ledgers"] = arrangement  # type: ignore[index]
        transaction = parse_account_billing(deepcopy(payload), ACCOUNT)[1]
        assert transaction is not None
        assert transaction.id == "transaction-1"


def test_a_transaction_without_dates_loses_to_one_with_them() -> None:
    payload = deepcopy(_billing())
    dateless = deepcopy(payload["account"]["ledgers"][0])  # type: ignore[index]
    dateless["transactions"]["edges"][0]["node"] |= {
        "id": "transaction-undated",
        "postedDate": None,
        "createdAt": None,
    }
    payload["account"]["ledgers"] = [dateless, payload["account"]["ledgers"][0]]  # type: ignore[index]

    transaction = parse_account_billing(payload, ACCOUNT)[1]

    assert transaction is not None
    assert transaction.id == "transaction-1"


def test_a_dateless_transaction_is_still_reported_when_it_is_the_only_one() -> None:
    """Dropping it would lose a transaction the customer can see in the provider's app."""
    payload = deepcopy(_billing())
    node = payload["account"]["ledgers"][0]["transactions"]["edges"][0]["node"]  # type: ignore[index]
    node |= {"postedDate": None, "createdAt": None}

    transaction = parse_account_billing(payload, ACCOUNT)[1]

    assert transaction is not None
    assert transaction.posted_date is None


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda account: account.update(ledgers=None), None),
        (lambda account: account.update(ledgers=[]), None),
        (lambda account: account["ledgers"][0].update(transactions=None), None),
    ],
    ids=["nulled-ledgers", "no-ledgers", "nulled-transactions"],
)
def test_a_partial_billing_response_keeps_the_bill_and_reports_no_transaction(
    mutate: object,
    expected: None,
) -> None:
    """A nulled sub-selection is the shape of a partial response, not a broken one.

    Raising here would discard the bill that arrived in the same response over a secondary
    field; the billing access record already tells the user the response was partial.
    """
    payload = deepcopy(_billing())
    mutate(payload["account"])  # type: ignore[operator, index]

    bill, transaction = parse_account_billing(payload, ACCOUNT)

    assert bill is not None
    assert transaction is expected


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda account: account.update(ledgers={"not": "a list"}), "malformed ledgers"),
        (lambda account: account.update(ledgers=["not a mapping"]), "malformed ledger"),
        (
            lambda account: account["ledgers"][0].update(transactions=["not a mapping"]),
            "malformed transactions",
        ),
    ],
    ids=["ledgers-not-a-list", "ledger-not-a-mapping", "transactions-not-a-mapping"],
)
def test_a_malformed_ledger_shape_is_rejected_rather_than_ignored(
    mutate: object,
    message: str,
) -> None:
    payload = deepcopy(_billing())
    mutate(payload["account"])  # type: ignore[operator, index]

    with pytest.raises(OejpInvalidResponseError, match=message):
        parse_account_billing(payload, ACCOUNT)


def _period_billing() -> dict[str, object]:
    """A real account's shape: the bill resolves as a period document, not a statement.

    The `bills` connection returns `PeriodBasedDocumentType` with `billType: STATEMENT`, so
    every field behind `... on StatementType` is absent. The default fixture uses
    `StatementType`, which is why the empty due date went unnoticed until it was measured.
    """
    payload = deepcopy(_billing())
    node = payload["account"]["bills"]["edges"][0]["node"]  # type: ignore[index]
    node.pop("statementTotalCharges")
    node.pop("paymentDueDate")
    node.pop("status")
    node.update(
        {
            "__typename": "PeriodBasedDocumentType",
            "periodTotalCharges": {"grossTotal": 8765},
            "periodIsAnnulled": False,
            "periodIsHeld": False,
        }
    )
    # The statement's id is an `Int` where the bill's is an `ID`, and they describe the same
    # document.
    payload["account"]["ledgers"][0]["statements"] = {  # type: ignore[index]
        "edges": [{"node": {"id": 1, "dueDate": "2026-07-31"}}]
    }
    node["id"] = "1"
    return payload


def test_the_due_date_comes_from_the_ledger_statement_when_the_bill_has_none() -> None:
    bill, _ = parse_account_billing(_period_billing(), ACCOUNT)

    assert bill is not None
    assert bill.type_name == "PeriodBasedDocumentType"
    assert bill.due_date is not None
    assert bill.due_date.isoformat() == "2026-07-31"


def test_the_statements_query_asks_for_the_newest_finalised_first() -> None:
    """Without an order the connection's first node is not the newest statement."""
    assert "statements(first: 1, orderBy: FINALIZED_AT_DESC)" in ACCOUNT_BILLING_QUERY


def test_a_statement_for_a_different_document_does_not_lend_its_due_date() -> None:
    """A due date from another billing period is worse than no due date at all."""
    payload = _period_billing()
    payload["account"]["ledgers"][0]["statements"]["edges"][0]["node"]["id"] = 99  # type: ignore[index]

    bill, _ = parse_account_billing(payload, ACCOUNT)

    assert bill is not None
    assert bill.due_date is None


def test_a_bill_that_carries_its_own_due_date_is_not_overwritten() -> None:
    payload = deepcopy(_billing())
    payload["account"]["ledgers"][0]["statements"] = {  # type: ignore[index]
        "edges": [{"node": {"id": "bill-1", "dueDate": "2001-01-01"}}]
    }

    bill, _ = parse_account_billing(payload, ACCOUNT)

    assert bill is not None
    assert bill.due_date is not None
    assert bill.due_date.isoformat() == "2026-07-31"


def test_the_bill_status_is_left_absent_rather_than_invented() -> None:
    """Nothing recovers `StatementType.status` for a period document.

    Measured on the real account: `documentDebtPosition` is null and
    `StatementBillingDocumentType.isFinal` is null, so there is no settled/outstanding
    signal to substitute. Reporting a guessed status would be worse than reporting none.
    """
    bill, _ = parse_account_billing(_period_billing(), ACCOUNT)

    assert bill is not None
    assert bill.status is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ledger: ledger.update(statements=None),
        lambda ledger: ledger.pop("statements"),
        lambda ledger: ledger["statements"].update(edges=[]),
        lambda ledger: ledger["statements"]["edges"][0]["node"].update(dueDate=None),
    ],
    ids=["nulled", "absent", "no-edges", "nulled-due-date"],
)
def test_an_unusable_statement_leaves_the_due_date_absent(mutate: object) -> None:
    payload = _period_billing()
    mutate(payload["account"]["ledgers"][0])  # type: ignore[operator, index]

    bill, _ = parse_account_billing(payload, ACCOUNT)

    assert bill is not None
    assert bill.due_date is None


def test_a_malformed_statements_connection_is_rejected() -> None:
    payload = _period_billing()
    payload["account"]["ledgers"][0]["statements"] = ["not a mapping"]  # type: ignore[index]

    with pytest.raises(OejpInvalidResponseError, match="malformed statements"):
        parse_account_billing(payload, ACCOUNT)


def test_more_statements_than_requested_is_rejected() -> None:
    payload = _period_billing()
    edges = payload["account"]["ledgers"][0]["statements"]["edges"]  # type: ignore[index]
    edges.append(deepcopy(edges[0]))

    with pytest.raises(OejpInvalidResponseError, match="result limit"):
        parse_account_billing(payload, ACCOUNT)


def test_more_bills_than_requested_is_rejected() -> None:
    """The bill excess guard is separate from the transaction one and needs its own case."""
    payload = deepcopy(_billing())
    edges = payload["account"]["bills"]["edges"]  # type: ignore[index]
    edges.append(deepcopy(edges[0]))

    with pytest.raises(OejpInvalidResponseError, match="result limit"):
        parse_account_billing(payload, ACCOUNT)


def test_a_period_bill_with_no_ledgers_at_all_keeps_an_absent_due_date() -> None:
    """The statement lookup must tolerate the partial response the transaction read does."""
    payload = _period_billing()
    payload["account"]["ledgers"] = None  # type: ignore[index]

    bill, transaction = parse_account_billing(payload, ACCOUNT)

    assert bill is not None
    assert bill.due_date is None
    assert transaction is None


def test_a_ledger_with_no_transactions_is_skipped_rather_than_ending_the_search() -> None:
    """A second ledger holding the only transaction must still be found."""
    payload = deepcopy(_billing())
    empty = {"transactions": {"edges": []}}
    payload["account"]["ledgers"] = [empty, payload["account"]["ledgers"][0]]  # type: ignore[index]

    _, transaction = parse_account_billing(payload, ACCOUNT)

    assert transaction is not None
    assert transaction.id == "transaction-1"


def test_a_statement_without_an_id_is_not_matched_to_the_bill() -> None:
    """`str(None)` is the truthy string "None", which must not be compared to an id."""
    payload = _period_billing()
    payload["account"]["ledgers"][0]["statements"]["edges"][0]["node"]["id"] = None  # type: ignore[index]

    bill, _ = parse_account_billing(payload, ACCOUNT)

    assert bill is not None
    assert bill.due_date is None
