"""Tests for optional account, product, bill, and transaction contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
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
                                "marketName": "ELECTRICITY",
                                "rates": [
                                    {
                                        "gridOperatorCode": "GRID",
                                        "regionOfOperation": "REGION",
                                        "band": "STANDARD",
                                        "validFrom": "2026-07-01T00:00:00+09:00",
                                        "validTo": None,
                                        "unitType": "KWH",
                                        "pricePerUnit": "31.25",
                                        "durationMonths": None,
                                    }
                                ],
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
                            "totalCharges": {"grossTotal": 8765},
                            "status": "CLOSED",
                            "isAnnulled": None,
                            "isHeld": None,
                        }
                    }
                ]
            },
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
            },
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
    assert agreements[-1].product.rates[0].price_per_unit == Decimal("31.25")
    assert bill is not None
    assert bill.gross_amount_minor == 8765
    assert bill.due_date is not None and bill.due_date.isoformat() == "2026-07-31"
    assert transaction is not None
    assert transaction.amount_minor == -8765
    assert transaction.created_at == datetime(2026, 7, 10, 1, 2, 3, tzinfo=UTC)


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
        lambda value: value["account"]["marketSupplyAgreements"]["edges"][0]["node"]["product"][
            "rates"
        ][0].update({"pricePerUnit": "NaN"}),
    ],
)
def test_agreements_reject_malformed_contract_data(mutation: object) -> None:
    payload = deepcopy(_agreements())
    mutation(payload)  # type: ignore[operator]
    with pytest.raises(OejpInvalidResponseError):
        parse_account_agreements(payload, ACCOUNT)


def test_billing_rejects_conflicting_or_excess_results() -> None:
    conflicting = deepcopy(_billing())
    bill = conflicting["account"]["bills"]["edges"][0]["node"]  # type: ignore[index]
    bill["grossAmount"] = 1
    with pytest.raises(OejpInvalidResponseError, match="conflicting gross"):
        parse_account_billing(conflicting, ACCOUNT)

    excess = deepcopy(_billing())
    edges = excess["account"]["transactions"]["edges"]  # type: ignore[index]
    edges.append(deepcopy(edges[0]))
    with pytest.raises(OejpInvalidResponseError, match="result limit"):
        parse_account_billing(excess, ACCOUNT)
