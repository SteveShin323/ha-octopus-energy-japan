"""Tests for strict OEJP resource and capability discovery."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from custom_components.octopus_energy_japan.api import (
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
    ResourceLifecycle,
    async_detect_capabilities,
    async_discover_generic_devices,
    async_discover_resources,
    async_paginate,
    attach_generic_devices,
    lifecycle_from_status,
    parse_capabilities,
    parse_generic_devices,
    parse_legacy_discovery,
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
                            "electricitySupplyPoints": [
                                {
                                    "id": "supply-2",
                                    "spin": "spin-2",
                                    "status": "OFF_SUPPLY",
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
