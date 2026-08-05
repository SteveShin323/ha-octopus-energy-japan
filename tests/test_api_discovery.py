"""Tests for strict OEJP resource and capability discovery."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from custom_components.octopus_energy_japan.api import (
    ACCOUNT_SUPPLY_PERIODS_QUERY,
    GENERIC_DEVICES_QUERY,
    LEGACY_DISCOVERY_QUERY,
    AuthenticatedGraphQLClient,
    Capability,
    CapabilityAvailability,
    ConnectionPage,
    GraphQLErrorDetail,
    GraphQLResult,
    OejpInvalidResponseError,
    OejpQueryValidationError,
    OejpSupplyPoint,
    ResourceLifecycle,
    async_detect_capabilities,
    async_discover_generic_devices,
    async_discover_resources,
    async_discover_supply_starts,
    async_paginate,
    attach_generic_devices,
    attach_supply_starts,
    lifecycle_from_status,
    parse_capabilities,
    parse_generic_devices,
    parse_legacy_discovery,
    parse_supply_starts,
)


def _discovery_payload() -> dict[str, object]:
    return {
        "viewer": {
            "accounts": [
                {
                    "number": "B-ACCOUNT",
                    "status": "CLOSED",
                    "properties": [],
                },
                {
                    "number": "A-ACCOUNT",
                    "status": "ACTIVE",
                    "properties": [
                        {
                            "id": "property-2",
                            "electricitySupplyPoints": [],
                        },
                        {
                            "id": "property-1",
                            "address": "PRIVATE-ADDRESS",
                            "postcode": "000-0000",
                            "electricitySupplyPoints": [
                                {
                                    "id": "supply-2",
                                    "spin": "spin-2",
                                    "status": "OFF_SUPPLY",
                                    "readingDateDayOfMonth": 19,
                                    "meters": [],
                                },
                                {
                                    "id": "supply-1",
                                    "spin": "spin-1",
                                    "status": "ON_SUPPLY",
                                    "meters": [
                                        {
                                            "serialNumber": "meter-2",
                                            "capacity": 60,
                                        },
                                        {
                                            "serialNumber": "meter-1",
                                            "capacity": None,
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                },
            ]
        }
    }


def test_legacy_discovery_returns_sorted_typed_hierarchy() -> None:
    accounts = parse_legacy_discovery(_discovery_payload())

    assert [account.number for account in accounts] == ["A-ACCOUNT", "B-ACCOUNT"]
    active = accounts[0]
    assert active.lifecycle is ResourceLifecycle.ACTIVE
    assert [property_.id for property_ in active.properties] == [
        "property-1",
        "property-2",
    ]
    points = active.properties[0].supply_points
    assert [point.id for point in points] == ["supply-1", "supply-2"]
    assert points[0].lifecycle is ResourceLifecycle.ACTIVE
    assert points[1].lifecycle is ResourceLifecycle.HISTORICAL
    assert [meter.serial_number for meter in points[0].meters] == [
        "meter-1",
        "meter-2",
    ]
    assert points[0].meters[1].capacity == "60"
    assert accounts[1].lifecycle is ResourceLifecycle.HISTORICAL


async def test_discover_resources_uses_strict_authorized_operation() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = _discovery_payload()

    accounts = await async_discover_resources(client)

    assert len(accounts) == 2
    client.execute.assert_awaited_once_with(LEGACY_DISCOVERY_QUERY)


async def test_generic_device_operation_validates_supply_point_identity() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = {
        "supplyPoint": {
            "externalIdentifier": "spin-1",
            "devices": {
                "edges": [
                    {
                        "node": {
                            "deviceIdentifier": "device-1",
                            "registers": {"edges": []},
                        }
                    }
                ]
            },
        }
    }

    devices = await async_discover_generic_devices(
        client,
        "spin-1",
    )

    assert devices[0].id == "device-1"
    client.execute.assert_awaited_once_with(
        GENERIC_DEVICES_QUERY,
        {
            "externalIdentifier": "spin-1",
            "marketName": "JPN_ELECTRICITY",
        },
    )


@pytest.mark.parametrize(
    "supply_point",
    [
        None,
        {},
        {"externalIdentifier": "different", "devices": {"edges": []}},
    ],
)
async def test_generic_device_operation_rejects_invalid_supply_point(
    supply_point: object,
) -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = {"supplyPoint": supply_point}

    with pytest.raises(OejpInvalidResponseError):
        await async_discover_generic_devices(
            client,
            "spin-1",
        )


def test_generic_devices_attach_to_matching_legacy_supply_point() -> None:
    accounts = parse_legacy_discovery(_discovery_payload())
    devices = parse_generic_devices(
        {
            "edges": [
                {
                    "node": {
                        "deviceIdentifier": "device-1",
                        "registers": {"edges": []},
                    }
                }
            ]
        }
    )

    enriched = attach_generic_devices(accounts, {"spin-1": devices})

    active_account = enriched[0]
    supply_points = active_account.properties[0].supply_points
    assert supply_points[0].devices == devices
    assert supply_points[1].devices == ()


def test_supply_point_can_fall_back_to_spin_identifier() -> None:
    payload = _discovery_payload()
    point = payload["viewer"]["accounts"][1]["properties"][1][  # type: ignore[index]
        "electricitySupplyPoints"
    ][0]
    point["id"] = None

    accounts = parse_legacy_discovery(payload)

    assert accounts[0].properties[0].supply_points[0].id == "spin-2"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (" active ", ResourceLifecycle.ACTIVE),
        ("on-supply", ResourceLifecycle.ACTIVE),
        ("OFF SUPPLY", ResourceLifecycle.HISTORICAL),
        ("terminated", ResourceLifecycle.HISTORICAL),
        ("unexpected", ResourceLifecycle.UNKNOWN),
        (None, ResourceLifecycle.UNKNOWN),
    ],
)
def test_lifecycle_normalization(
    status: str | None,
    expected: ResourceLifecycle,
) -> None:
    assert lifecycle_from_status(status) is expected


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.clear(),
        lambda payload: payload["viewer"].update({"accounts": {}}),
        lambda payload: payload["viewer"]["accounts"].append(None),
        lambda payload: payload["viewer"]["accounts"][0].update({"number": ""}),
        lambda payload: payload["viewer"]["accounts"][0].update({"properties": None}),
        lambda payload: payload["viewer"]["accounts"][1]["properties"].append(None),
        lambda payload: payload["viewer"]["accounts"][1]["properties"][0].update({"id": ""}),
        lambda payload: payload["viewer"]["accounts"][1]["properties"][0].update(
            {"electricitySupplyPoints": None}
        ),
        lambda payload: payload["viewer"]["accounts"][1]["properties"][1][
            "electricitySupplyPoints"
        ].append(None),
        lambda payload: payload["viewer"]["accounts"][1]["properties"][1][
            "electricitySupplyPoints"
        ][0].update({"id": None, "spin": None}),
        lambda payload: payload["viewer"]["accounts"][1]["properties"][1][
            "electricitySupplyPoints"
        ][0].update({"meters": None}),
        lambda payload: payload["viewer"]["accounts"][1]["properties"][1][
            "electricitySupplyPoints"
        ][1]["meters"].append(None),
        lambda payload: payload["viewer"]["accounts"][1]["properties"][1][
            "electricitySupplyPoints"
        ][1]["meters"][0].update({"serialNumber": ""}),
        lambda payload: payload["viewer"]["accounts"][1]["properties"][1][
            "electricitySupplyPoints"
        ][1]["meters"][0].update({"capacity": {}}),
    ],
)
def test_legacy_discovery_rejects_malformed_nested_resources(mutate: object) -> None:
    payload = _discovery_payload()
    mutate(payload)  # type: ignore[operator]

    with pytest.raises(OejpInvalidResponseError):
        parse_legacy_discovery(payload)


def test_conflicting_duplicates_are_rejected_but_exact_duplicates_are_safe() -> None:
    payload = _discovery_payload()
    accounts = payload["viewer"]["accounts"]  # type: ignore[index]
    accounts.append(dict(accounts[0]))
    assert len(parse_legacy_discovery(payload)) == 2

    accounts[-1] = {**accounts[0], "status": "ACTIVE"}
    with pytest.raises(OejpInvalidResponseError, match="duplicate account"):
        parse_legacy_discovery(payload)


def _capability_payload() -> dict[str, object]:
    return {
        "queryType": {"fields": [{"name": "supplyPoint"}, {"name": "supplyPoints"}]},
        "supplyPointType": {"fields": [{"name": "readings"}, {"name": "devices"}]},
        "deviceType": {"fields": [{"name": "registers"}]},
        "readingsType": {"fields": [{"name": "importReadings"}, {"name": "exportReadings"}]},
        "readingType": {"fields": [{"name": "qualities"}]},
        "legacySupplyPointType": {
            "fields": [
                {"name": "halfHourlyReadings"},
                {"name": "intervalReadings"},
            ]
        },
    }


def test_capability_registry_detects_all_supported_reading_features() -> None:
    snapshot = parse_capabilities(_capability_payload())

    for capability in Capability:
        assert snapshot.availability(capability) is CapabilityAvailability.SUPPORTED


def test_missing_capability_is_explicit_and_unknown_is_default() -> None:
    snapshot = parse_capabilities(
        {
            "queryType": None,
            "supplyPointType": None,
            "deviceType": None,
            "readingsType": None,
            "readingType": None,
            "legacySupplyPointType": None,
        }
    )

    assert snapshot.availability(Capability.GENERIC_READINGS) is CapabilityAvailability.UNSUPPORTED


async def test_capability_authorization_failure_does_not_trigger_reauth() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    detail = GraphQLErrorDetail(
        message="GraphQL operation failed",
        error_type="AUTHORIZATION",
    )
    client.execute_optional.return_value = GraphQLResult(None, (detail,))

    snapshot = await async_detect_capabilities(client)

    assert all(
        status.availability is CapabilityAvailability.FORBIDDEN for status in snapshot.statuses
    )


async def test_partial_capability_response_marks_missing_fields_forbidden() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    detail = GraphQLErrorDetail(
        message="GraphQL operation failed",
        error_type="AUTHORIZATION",
    )
    client.execute_optional.return_value = GraphQLResult(
        {"queryType": {"fields": [{"name": "supplyPoints"}]}},
        (detail,),
    )

    snapshot = await async_detect_capabilities(client)

    assert snapshot.availability(Capability.GENERIC_READINGS) is CapabilityAvailability.FORBIDDEN


async def test_non_authorization_capability_error_is_raised() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    detail = GraphQLErrorDetail(
        message="GraphQL operation failed",
        error_type="VALIDATION",
    )
    client.execute_optional.return_value = GraphQLResult(None, (detail,))

    with pytest.raises(OejpQueryValidationError, match="graphql operation failed"):
        await async_detect_capabilities(client)


async def test_capability_response_requires_data() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute_optional.return_value = GraphQLResult(None, ())

    with pytest.raises(OejpInvalidResponseError, match="did not contain data"):
        await async_detect_capabilities(client)


def test_generic_devices_and_registers_are_sorted_and_typed() -> None:
    devices = parse_generic_devices(
        {
            "edges": [
                {
                    "node": {
                        "id": "device-2",
                        "deviceType": "METER",
                        "registers": {"edges": []},
                    }
                },
                {
                    "node": {
                        "id": "device-1",
                        "registers": {
                            "edges": [
                                {"node": {"id": "register-2", "unit": "kWh"}},
                                {"node": {"id": "register-1", "unit": None}},
                            ]
                        },
                    }
                },
            ]
        }
    )

    assert [device.id for device in devices] == ["device-1", "device-2"]
    assert [register.id for register in devices[0].registers] == [
        "register-1",
        "register-2",
    ]


def test_generic_devices_use_documented_provider_identifiers() -> None:
    devices = parse_generic_devices(
        {
            "edges": [
                {
                    "node": {
                        "deviceIdentifier": "device-provider",
                        "registers": {
                            "edges": [
                                {
                                    "node": {
                                        "registerIdentifier": "register-provider",
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        }
    )

    assert devices[0].id == "device-provider"
    assert devices[0].registers[0].id == "register-provider"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"edges": None},
        {"edges": [None]},
        {"edges": [{}]},
        {"edges": [{"node": None}]},
        {"edges": [{"node": {"id": "", "registers": {"edges": []}}}]},
        {"edges": [{"node": {"id": "device", "registers": None}}]},
        {
            "edges": [
                {
                    "node": {
                        "id": "device",
                        "registers": {"edges": [None]},
                    }
                }
            ]
        },
        {
            "edges": [
                {
                    "node": {
                        "id": "device",
                        "registers": {"edges": [{}]},
                    }
                }
            ]
        },
    ],
)
def test_generic_device_parser_rejects_malformed_connections(
    payload: object,
) -> None:
    with pytest.raises(OejpInvalidResponseError):
        parse_generic_devices(payload)


async def test_paginator_collects_pages_in_order() -> None:
    fetch = AsyncMock(
        side_effect=[
            ConnectionPage(("a",), True, "cursor-1"),
            ConnectionPage(("b", "c"), False, None),
        ]
    )

    assert await async_paginate(fetch) == ("a", "b", "c")
    assert [call.args[0] for call in fetch.await_args_list] == [None, "cursor-1"]


@pytest.mark.parametrize(
    "pages",
    [
        [ConnectionPage((), True, None)],
        [
            ConnectionPage((), True, "same"),
            ConnectionPage((), True, "same"),
        ],
    ],
)
async def test_paginator_rejects_invalid_cursor_progress(
    pages: list[ConnectionPage[object]],
) -> None:
    with pytest.raises(OejpInvalidResponseError):
        await async_paginate(AsyncMock(side_effect=pages))


async def test_paginator_enforces_positive_page_limit_and_bound() -> None:
    with pytest.raises(ValueError):
        await async_paginate(AsyncMock(), max_pages=0)
    with pytest.raises(OejpInvalidResponseError, match="safety limit"):
        await async_paginate(
            AsyncMock(return_value=ConnectionPage((), True, "next")),
            max_pages=1,
        )


def test_page_sizes_respect_the_published_pagination_limit() -> None:
    """The official guide requires `first` to be less than 100."""
    from custom_components.octopus_energy_japan.api import (
        ACCOUNT_AGREEMENTS_QUERY,
        ACCOUNT_BILLING_QUERY,
    )
    from custom_components.octopus_energy_japan.api.readings import GENERIC_PAGE_SIZE

    published_limit = 100
    assert published_limit > GENERIC_PAGE_SIZE

    for document in (ACCOUNT_AGREEMENTS_QUERY, ACCOUNT_BILLING_QUERY, GENERIC_DEVICES_QUERY):
        for requested in re.findall(r"first:\s*(\d+)", document):
            assert int(requested) < published_limit, document


def test_rate_limit_codes_cover_every_published_limit() -> None:
    """Complexity, node, and request-rate limits must all back off."""
    from custom_components.octopus_energy_japan.api.errors import _RATE_LIMIT_CODES

    assert {"KT-CT-1188", "KT-CT-1189", "KT-CT-1199"} <= _RATE_LIMIT_CODES


def test_the_property_address_and_the_reading_day_are_carried_through() -> None:
    accounts = parse_legacy_discovery(_discovery_payload())

    property_ = next(p for a in accounts for p in a.properties if p.id == "property-1")
    assert property_.address == "PRIVATE-ADDRESS"
    assert property_.postcode == "000-0000"
    point = next(point for point in property_.supply_points if point.id == "supply-2")
    assert point.reading_day_of_month == 19


def test_a_property_without_an_address_is_still_parsed() -> None:
    """The fields are additions; an account that omits them must not fail discovery."""
    payload = _discovery_payload()
    property_ = payload["viewer"]["accounts"][1]["properties"][1]  # type: ignore[index]
    property_.pop("address")
    property_.pop("postcode")

    accounts = parse_legacy_discovery(payload)

    parsed = next(p for a in accounts for p in a.properties if p.id == "property-1")
    assert parsed.address is None
    assert parsed.postcode is None


@pytest.mark.parametrize("value", [0, 32, -1, True, "19", 19.0, None])
def test_a_reading_day_that_is_not_a_day_of_the_month_is_dropped(value: object) -> None:
    """A reading day of 0 or 40 is worse than none: an automation would act on it."""
    payload = _discovery_payload()
    point = payload["viewer"]["accounts"][1]["properties"][1]["electricitySupplyPoints"][0]  # type: ignore[index]
    point["readingDateDayOfMonth"] = value

    accounts = parse_legacy_discovery(payload)

    parsed = next(
        p
        for a in accounts
        for prop in a.properties
        for p in prop.supply_points
        if p.id == "supply-2"
    )
    assert parsed.reading_day_of_month is None


@pytest.mark.parametrize("value", [1, 19, 31])
def test_every_real_day_of_the_month_is_accepted(value: int) -> None:
    payload = _discovery_payload()
    point = payload["viewer"]["accounts"][1]["properties"][1]["electricitySupplyPoints"][0]  # type: ignore[index]
    point["readingDateDayOfMonth"] = value

    accounts = parse_legacy_discovery(payload)

    parsed = next(
        p
        for a in accounts
        for prop in a.properties
        for p in prop.supply_points
        if p.id == "supply-2"
    )
    assert parsed.reading_day_of_month == value


def _parsed_supply_point(payload: dict[str, Any], point_id: str) -> OejpSupplyPoint:
    return next(
        point
        for account in parse_legacy_discovery(payload)
        for property_ in account.properties
        for point in property_.supply_points
        if point.id == point_id
    )


def _supply_periods_payload(periods: object) -> dict[str, object]:
    return {
        "account": {
            "number": "A-ACCOUNT",
            "properties": [
                {
                    "electricitySupplyPoints": [
                        {"id": "supply-1", "spin": "spin-1", "supplyPeriods": periods},
                    ]
                }
            ],
        }
    }


def test_supply_started_at_the_earliest_billable_period() -> None:
    """This anchors the billing period the tariff accumulates over.

    A supply point can carry several periods — a move out and back in, or a meter exchange —
    and the charging schedule begins at the first one the customer is billed for.
    """
    starts = parse_supply_starts(
        _supply_periods_payload(
            [
                {
                    "supplyStartAt": "2026-09-01T15:00:00+00:00",
                    "supplyEndAt": None,
                    "isBillable": True,
                },
                {
                    "supplyStartAt": "2026-06-17T15:00:00+00:00",
                    "supplyEndAt": "2026-08-31T15:00:00+00:00",
                    "isBillable": True,
                },
            ]
        )
    )

    assert starts == {
        "supply-1": datetime(2026, 6, 17, 15, tzinfo=UTC),
        "spin-1": datetime(2026, 6, 17, 15, tzinfo=UTC),
    }


def test_a_period_the_customer_is_not_billed_for_cannot_start_the_schedule() -> None:
    """A non-billable period is a gap no charge accrues over, so it anchors nothing."""
    starts = parse_supply_starts(
        _supply_periods_payload(
            [
                {"supplyStartAt": "2026-05-17T15:00:00+00:00", "isBillable": False},
                {"supplyStartAt": "2026-06-17T15:00:00+00:00", "isBillable": True},
            ]
        )
    )

    assert starts["supply-1"] == datetime(2026, 6, 17, 15, tzinfo=UTC)


def test_a_naive_supply_start_is_read_as_utc() -> None:
    starts = parse_supply_starts(
        _supply_periods_payload([{"supplyStartAt": "2026-06-17T15:00:00", "isBillable": True}])
    )

    assert starts["supply-1"] == datetime(2026, 6, 17, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    "periods",
    [
        None,
        [],
        "not-a-list",
        [{"supplyStartAt": "not-a-date", "isBillable": True}],
        [{"supplyStartAt": None, "isBillable": True}],
        [{"supplyStartAt": "2026-06-17T15:00:00+00:00", "isBillable": None}],
        [{"supplyStartAt": "2026-06-17T15:00:00+00:00"}],
        ["not-a-mapping"],
    ],
)
def test_an_unusable_supply_period_leaves_the_calendar_month_in_charge(
    periods: object,
) -> None:
    """The fallback is what the cost formula used before this field existed.

    Raising would fail setup over a field consumption does not need, and consumption is the
    part a user cannot do without.
    """
    assert parse_supply_starts(_supply_periods_payload(periods)) == {}


_BILLABLE = [{"supplyStartAt": "2026-06-17T15:00:00+00:00", "isBillable": True}]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"account": None},
        {"account": {}},
        {"account": {"properties": None}},
        {"account": {"properties": ["not-a-mapping"]}},
        {"account": {"properties": [{"electricitySupplyPoints": None}]}},
        {"account": {"properties": [{"electricitySupplyPoints": ["not-a-mapping"]}]}},
        # A partial response can null the identifiers while still returning the periods, and
        # a start with nothing to attach it to is not usable.
        {"account": {"properties": [{"electricitySupplyPoints": [{"supplyPeriods": _BILLABLE}]}]}},
    ],
)
def test_a_malformed_supply_period_response_yields_nothing(payload: dict[str, object]) -> None:
    assert parse_supply_starts(payload) == {}


async def test_supply_starts_are_optional_when_the_account_may_not_read_them() -> None:
    """Authorization for this field depends on the path it is reached by.

    Measured on a real account: `viewer.accounts` returns AUTHORIZATION/KT-CT-4501 while
    `account(accountNumber:)` returns data, which is why it is asked account-scoped and why a
    refusal has to degrade rather than fail.
    """
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute_optional.return_value = GraphQLResult(
        data=None,
        errors=(
            GraphQLErrorDetail(
                message="Unauthorized.",
                error_type="AUTHORIZATION",
                error_code="KT-CT-4501",
                path=("account", "properties", 0, "electricitySupplyPoints", 0, "supplyPeriods"),
            ),
        ),
    )

    assert await async_discover_supply_starts(client, "A-ACCOUNT") == {}
    client.execute_optional.assert_awaited_once_with(
        ACCOUNT_SUPPLY_PERIODS_QUERY,
        {"accountNumber": "A-ACCOUNT"},
    )


async def test_supply_starts_are_returned_when_the_account_may_read_them() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute_optional.return_value = GraphQLResult(
        data=_supply_periods_payload(
            [{"supplyStartAt": "2026-06-17T15:00:00+00:00", "isBillable": True}]
        )
    )

    starts = await async_discover_supply_starts(client, "A-ACCOUNT")

    assert starts["supply-1"] == datetime(2026, 6, 17, 15, tzinfo=UTC)


def test_a_supply_start_reaches_the_supply_point_it_belongs_to() -> None:
    accounts = parse_legacy_discovery(_discovery_payload())
    start = datetime(2026, 6, 17, 15, tzinfo=UTC)

    enriched = attach_supply_starts(accounts, {"supply-1": start})

    points = {
        point.id: point
        for account in enriched
        for property_ in account.properties
        for point in property_.supply_points
    }
    assert points["supply-1"].supply_start_at == start
    assert points["supply-2"].supply_start_at is None


def test_a_supply_start_can_be_keyed_by_the_spin_instead() -> None:
    """The discovery tree identifies a point by whichever identifier the provider returned."""
    accounts = parse_legacy_discovery(_discovery_payload())
    start = datetime(2026, 6, 17, 15, tzinfo=UTC)

    enriched = attach_supply_starts(accounts, {"spin-2": start})

    points = {
        point.id: point
        for account in enriched
        for property_ in account.properties
        for point in property_.supply_points
    }
    assert points["supply-2"].supply_start_at == start


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        # Measured on a real account: both scheduled dates fell on the 18th, one month apart,
        # and the closed invoice ran from the 18th to the 17th.
        ("2026-06-18", "2026-07-18", 18),
        (None, "2026-07-18", None),
        ("2026-06-18", None, None),
        # Days that disagree say nothing certain, including the short-month case.
        ("2026-01-31", "2026-02-28", None),
        # A gap this calendar does not model.
        ("2026-06-18", "2026-08-18", None),
        ("2026-07-18", "2026-06-18", None),
        ("not-a-date", "2026-07-18", None),
    ],
)
def test_the_reading_schedule_day_needs_two_dates_that_agree(
    first: object,
    second: object,
    expected: int | None,
) -> None:
    """Two consecutive dates on the same day are the recurring schedule stated twice.

    One date alone, or a pair that disagrees, is not evidence of a recurring day, and the
    billing anchor derived from it would silently shift every step boundary.
    """
    payload = _discovery_payload()
    point = payload["viewer"]["accounts"][1]["properties"][1]["electricitySupplyPoints"][0]  # type: ignore[index]
    point["nextReadingDate"] = first
    point["nextNextReadingDate"] = second

    assert _parsed_supply_point(payload, "supply-2").reading_schedule_day == expected
