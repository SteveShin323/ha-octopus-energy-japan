"""Contract and policy tests for OEJP reading providers."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from custom_components.octopus_energy_japan.api import (
    LEGACY_HALF_HOURLY_QUERY,
    LEGACY_INTERVAL_QUERY,
    AuthenticatedGraphQLClient,
    Capability,
    CapabilityAvailability,
    CapabilitySnapshot,
    CapabilityStatus,
    EnergyUnit,
    GenericReadingsProvider,
    GenericReadingTarget,
    GenericUnavailableReason,
    GraphQLErrorDetail,
    LegacyHalfHourlyProvider,
    OejpAuthenticationError,
    OejpAuthorizationError,
    OejpDevice,
    OejpGenericProviderUnavailableError,
    OejpInvalidResponseError,
    OejpNoReadingProviderError,
    OejpNotFoundError,
    OejpQueryValidationError,
    OejpRateLimitError,
    OejpRegister,
    OejpSupplyPoint,
    OejpTimeoutError,
    OejpTransportError,
    ReadingDirection,
    ReadingFallbackReason,
    ReadingGranularity,
    ReadingProviderName,
    ReadingProviderRouter,
    ReadingSource,
    build_generic_readings_query,
    parse_generic_readings_page,
    parse_legacy_half_hourly_readings,
    parse_legacy_interval_readings,
)
from custom_components.octopus_energy_japan.probe import (
    assert_contract_provenance,
    assert_safe_fixture,
)

START = datetime(2026, 7, 1, tzinfo=UTC)
END = START + timedelta(hours=1)
FETCHED = datetime(2026, 7, 29, 12, tzinfo=UTC)
FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "contracts"


def _capabilities(
    *,
    generic: CapabilityAvailability = CapabilityAvailability.SUPPORTED,
    half_hourly: CapabilityAvailability = CapabilityAvailability.SUPPORTED,
    interval: CapabilityAvailability = CapabilityAvailability.SUPPORTED,
    import_: CapabilityAvailability = CapabilityAvailability.SUPPORTED,
    export: CapabilityAvailability = CapabilityAvailability.UNSUPPORTED,
    quality: CapabilityAvailability = CapabilityAvailability.SUPPORTED,
) -> CapabilitySnapshot:
    values = {
        Capability.GENERIC_READINGS: generic,
        Capability.LEGACY_HALF_HOURLY_READINGS: half_hourly,
        Capability.LEGACY_INTERVAL_READINGS: interval,
        Capability.IMPORT_READINGS: import_,
        Capability.EXPORT_READINGS: export,
        Capability.READING_QUALITY: quality,
    }
    return CapabilitySnapshot(
        tuple(
            CapabilityStatus(capability, availability)
            for capability, availability in values.items()
        )
    )


def _point(
    *,
    devices: tuple[OejpDevice, ...] = (),
    direction: ReadingDirection = ReadingDirection.UNKNOWN,
) -> OejpSupplyPoint:
    return OejpSupplyPoint(
        id="supply-id",
        spin="spin-id",
        account_number="account-id",
        direction=direction,
        devices=devices,
    )


def _generic_node(
    *,
    start: str = "2026-07-01T00:00:00Z",
    end: str = "2026-07-01T00:30:00Z",
    value: object = "0.375",
    units: object = "KILOWATT_HOURS",
    qualities: object = None,
) -> dict[str, object]:
    node: dict[str, object] = {
        "intervalStart": start,
        "intervalEnd": end,
        "value": value,
        "units": units,
    }
    if qualities is not None:
        node["qualities"] = qualities
    return node


def _connection(
    nodes: list[dict[str, object]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    return {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "edges": [{"node": node} for node in nodes],
    }


def _generic_payload(
    nodes: list[dict[str, object]],
    *,
    direction: ReadingDirection = ReadingDirection.IMPORT,
    target: GenericReadingTarget | None = None,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    target = target or GenericReadingTarget()
    field = "importReadings" if direction is ReadingDirection.IMPORT else "exportReadings"
    series: dict[str, object] = {
        "readings": {field: _connection(nodes, has_next=has_next, cursor=cursor)}
    }
    if target.register_id is not None:
        series = {
            "registers": {
                "edges": [
                    {
                        "node": {
                            "registerIdentifier": target.register_id,
                            **series,
                        }
                    }
                ]
            }
        }
    if target.device_id is not None:
        series = {
            "devices": {
                "edges": [
                    {
                        "node": {
                            "deviceIdentifier": target.device_id,
                            **series,
                        }
                    }
                ]
            }
        }
    return {
        "supplyPoint": {
            "externalIdentifier": "spin-id",
            **series,
        }
    }


def _legacy_payload(
    *,
    half_hourly: list[dict[str, object]] | None = None,
    interval: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    point: dict[str, object] = {"id": "supply-id", "spin": "spin-id"}
    if half_hourly is not None:
        point["halfHourlyReadings"] = half_hourly
    if interval is not None:
        point["intervalReadings"] = interval
    return {
        "account": {
            "number": "account-id",
            "properties": [{"electricitySupplyPoints": [point]}],
        }
    }


def _load_contract_fixture(name: str) -> dict[str, object]:
    fixture = json.loads((FIXTURE_DIRECTORY / name).read_text())
    assert isinstance(fixture, dict)
    assert_contract_provenance(fixture)
    assert_safe_fixture(fixture)
    return fixture


def test_generic_contract_fixture_matches_query_and_parser() -> None:
    fixture = _load_contract_fixture("generic_import_readings.json")
    query = build_generic_readings_query(
        "supply_point",
        ReadingDirection.IMPORT,
        True,
    )
    assert fixture["_meta"]["query_sha256"] == hashlib.sha256(query.encode()).hexdigest()  # type: ignore[index]
    page = parse_generic_readings_page(
        fixture["response"],  # type: ignore[arg-type]
        supply_point=OejpSupplyPoint(
            id="<synthetic:identifier:1>",
            spin="<synthetic:supply_point:1>",
            account_number="<synthetic:account:1>",
        ),
        external_identifier="<synthetic:supply_point:1>",
        target=GenericReadingTarget(),
        direction=ReadingDirection.IMPORT,
        fetched_at=FETCHED,
        include_quality=True,
    )
    assert page.items[0].value == Decimal("0.375")


@pytest.mark.parametrize(
    ("name", "query", "parser", "source"),
    [
        (
            "legacy_half_hourly_readings.json",
            LEGACY_HALF_HOURLY_QUERY,
            parse_legacy_half_hourly_readings,
            ReadingSource.LEGACY_HALF_HOURLY,
        ),
        (
            "legacy_interval_readings.json",
            LEGACY_INTERVAL_QUERY,
            parse_legacy_interval_readings,
            ReadingSource.LEGACY_INTERVAL,
        ),
    ],
)
def test_legacy_contract_fixtures_match_queries_and_parsers(
    name: str,
    query: str,
    parser: object,
    source: ReadingSource,
) -> None:
    fixture = _load_contract_fixture(name)
    assert fixture["_meta"]["query_sha256"] == hashlib.sha256(query.encode()).hexdigest()  # type: ignore[index]
    readings = parser(  # type: ignore[operator]
        fixture["response"],
        supply_point=OejpSupplyPoint(
            id="<synthetic:identifier:1>",
            spin="<synthetic:supply_point:1>",
            account_number="<synthetic:account:1>",
        ),
        fetched_at=FETCHED,
    )
    assert readings[0].source is source


@pytest.mark.parametrize("target", ["supply_point", "device", "register"])
@pytest.mark.parametrize("direction", [ReadingDirection.IMPORT, ReadingDirection.EXPORT])
@pytest.mark.parametrize("quality", [False, True])
def test_generic_query_covers_every_target_direction_and_quality_variant(
    target: str,
    direction: ReadingDirection,
    quality: bool,
) -> None:
    query = build_generic_readings_query(target, direction, quality)

    assert "readingType: INTERVAL" in query
    assert "timeGranularity: THIRTY_MIN" in query
    assert 'timezone: "UTC"' in query
    assert ("importReadings" in query) is (direction is ReadingDirection.IMPORT)
    assert ("exportReadings" in query) is (direction is ReadingDirection.EXPORT)
    assert ("qualities { quality value count }" in query) is quality
    assert ("deviceIdentifiers" in query) is (target != "supply_point")
    assert ("registerIdentifiers" in query) is (target == "register")


def test_generic_page_parser_preserves_unit_quality_direction_and_utc() -> None:
    page = parse_generic_readings_page(
        _generic_payload(
            [
                _generic_node(
                    start="2026-07-01T09:00:00+09:00",
                    end="2026-07-01T09:30:00+09:00",
                    units="kWh",
                    qualities=[
                        {"quality": "ESTIMATE", "value": None, "count": 0},
                        {"quality": "ACTUAL", "value": "0.375", "count": 1},
                    ],
                )
            ],
            has_next=True,
            cursor="cursor-1",
        ),
        supply_point=_point(),
        external_identifier="spin-id",
        target=GenericReadingTarget(),
        direction=ReadingDirection.IMPORT,
        fetched_at=FETCHED,
        include_quality=True,
    )

    assert page.has_next_page is True
    assert page.end_cursor == "cursor-1"
    assert len(page.items) == 1
    reading = page.items[0]
    assert reading.account_id == "account-id"
    assert reading.start_at == START
    assert reading.end_at == START + timedelta(minutes=30)
    assert reading.value == Decimal("0.375")
    assert reading.unit is EnergyUnit.KWH
    assert reading.granularity is ReadingGranularity.THIRTY_MIN
    assert reading.direction is ReadingDirection.IMPORT
    assert reading.source is ReadingSource.SUPPLY_POINT_READINGS
    assert [quality.code for quality in reading.qualities] == ["ACTUAL", "ESTIMATE"]
    assert reading.qualities[0].value == Decimal("0.375")
    assert reading.qualities[0].count == 1


def test_generic_page_parser_selects_exact_register_series() -> None:
    target = GenericReadingTarget(device_id="device-1", register_id="register-1")
    page = parse_generic_readings_page(
        _generic_payload([_generic_node()], target=target),
        supply_point=_point(),
        external_identifier="spin-id",
        target=target,
        direction=ReadingDirection.IMPORT,
        fetched_at=FETCHED,
        include_quality=False,
    )

    assert page.items[0].device_id == "device-1"
    assert page.items[0].register_id == "register-1"


@pytest.mark.parametrize("page_size", [0, 100])
def test_generic_provider_rejects_unsafe_page_size(page_size: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 99"):
        GenericReadingsProvider(
            AsyncMock(spec=AuthenticatedGraphQLClient),
            _capabilities(),
            page_size=page_size,
        )


@pytest.mark.parametrize(
    ("point", "capabilities", "start", "end", "error"),
    [
        (
            _point(),
            _capabilities(generic=CapabilityAvailability.UNSUPPORTED),
            START,
            END,
            OejpGenericProviderUnavailableError,
        ),
        (
            _point(),
            _capabilities(
                import_=CapabilityAvailability.UNSUPPORTED,
                export=CapabilityAvailability.UNSUPPORTED,
            ),
            START,
            END,
            OejpGenericProviderUnavailableError,
        ),
        (
            OejpSupplyPoint(id="", account_number="account"),
            _capabilities(),
            START,
            END,
            OejpInvalidResponseError,
        ),
        (
            _point(),
            _capabilities(),
            START.replace(tzinfo=None),
            END,
            ValueError,
        ),
        (
            _point(),
            _capabilities(),
            END,
            START,
            ValueError,
        ),
    ],
)
async def test_generic_provider_validates_capability_identity_and_window(
    point: OejpSupplyPoint,
    capabilities: CapabilitySnapshot,
    start: datetime,
    end: datetime,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        await GenericReadingsProvider(
            AsyncMock(spec=AuthenticatedGraphQLClient),
            capabilities,
            now=lambda: FETCHED,
        ).async_get_readings(point, start, end)


async def test_generic_provider_validates_provider_clock() -> None:
    with pytest.raises(ValueError, match="Provider clock"):
        await GenericReadingsProvider(
            AsyncMock(spec=AuthenticatedGraphQLClient),
            _capabilities(),
            now=lambda: FETCHED.replace(tzinfo=None),
        ).async_get_readings(_point(), START, END)


async def test_generic_provider_paginates_with_utc_window_and_stable_fetch_time() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.side_effect = [
        _generic_payload([_generic_node()], has_next=True, cursor="next"),
        _generic_payload(
            [
                _generic_node(
                    start="2026-07-01T00:30:00Z",
                    end="2026-07-01T01:00:00Z",
                    value="0.5",
                )
            ]
        ),
    ]
    provider = GenericReadingsProvider(
        client,
        _capabilities(quality=CapabilityAvailability.UNSUPPORTED),
        now=lambda: FETCHED,
        page_size=10,
    )

    readings = await provider.async_get_readings(
        _point(),
        START.astimezone(timezone(timedelta(hours=9))),
        END.astimezone(timezone(timedelta(hours=9))),
    )

    assert [reading.value for reading in readings] == [Decimal("0.375"), Decimal("0.5")]
    assert all(reading.fetched_at == FETCHED for reading in readings)
    first_variables = client.execute.await_args_list[0].args[1]
    second_variables = client.execute.await_args_list[1].args[1]
    assert first_variables["startAt"] == "2026-07-01T00:00:00Z"
    assert first_variables["endAt"] == "2026-07-01T01:00:00Z"
    assert first_variables["after"] is None
    assert second_variables["after"] == "next"
    assert first_variables["first"] == 10


async def test_generic_provider_prefers_register_series_over_parent_totals() -> None:
    target = GenericReadingTarget(device_id="device-1", register_id="register-1")
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = _generic_payload([_generic_node()], target=target)
    point = _point(
        devices=(
            OejpDevice(
                id="device-1",
                registers=(OejpRegister(id="register-1"),),
            ),
        )
    )

    readings = await GenericReadingsProvider(
        client,
        _capabilities(quality=CapabilityAvailability.UNSUPPORTED),
        now=lambda: FETCHED,
    ).async_get_readings(point, START, END)

    assert len(readings) == 1
    assert readings[0].register_id == "register-1"
    assert "RegisterImportReadings" in client.execute.await_args.args[0]


async def test_generic_provider_reads_device_export_series() -> None:
    target = GenericReadingTarget(device_id="device-1")
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = _generic_payload(
        [_generic_node(units="MEGAWATT_HOURS")],
        direction=ReadingDirection.EXPORT,
        target=target,
    )
    point = _point(devices=(OejpDevice(id="device-1"),))

    readings = await GenericReadingsProvider(
        client,
        _capabilities(
            import_=CapabilityAvailability.UNSUPPORTED,
            export=CapabilityAvailability.SUPPORTED,
            quality=CapabilityAvailability.UNSUPPORTED,
        ),
        now=lambda: FETCHED,
    ).async_get_readings(point, START, END)

    assert readings[0].device_id == "device-1"
    assert readings[0].direction is ReadingDirection.EXPORT
    assert readings[0].unit is EnergyUnit.MWH
    assert "DeviceExportReadings" in client.execute.await_args.args[0]


async def test_generic_provider_retries_without_forbidden_optional_quality() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.side_effect = [
        OejpAuthorizationError(
            (
                GraphQLErrorDetail(
                    "safe",
                    error_type="AUTHORIZATION",
                    path=("supplyPoint", "readings", "importReadings", 0, "qualities"),
                ),
            )
        ),
        _generic_payload([_generic_node()]),
    ]

    readings = await GenericReadingsProvider(
        client,
        _capabilities(),
        now=lambda: FETCHED,
    ).async_get_readings(_point(), START, END)

    assert len(readings) == 1
    assert readings[0].qualities == ()
    assert client.execute.await_count == 2
    assert "qualities" in client.execute.await_args_list[0].args[0]
    assert "qualities" not in client.execute.await_args_list[1].args[0]


@pytest.mark.parametrize(
    "detail",
    [
        GraphQLErrorDetail("safe", error_type="AUTHORIZATION"),
        GraphQLErrorDetail(
            "safe",
            error_type="AUTHORIZATION",
            path=("supplyPoint", "readings"),
        ),
    ],
)
async def test_generic_provider_does_not_retry_unscoped_authorization(
    detail: GraphQLErrorDetail,
) -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.side_effect = OejpAuthorizationError((detail,))

    with pytest.raises(OejpAuthorizationError):
        await GenericReadingsProvider(
            client,
            _capabilities(),
            now=lambda: FETCHED,
        ).async_get_readings(_point(), START, END)
    assert client.execute.await_count == 1


def test_exact_generic_duplicate_is_deduplicated_and_conflict_is_rejected() -> None:
    duplicate = _generic_node()
    page = parse_generic_readings_page(
        _generic_payload([duplicate, deepcopy(duplicate)]),
        supply_point=_point(),
        external_identifier="spin-id",
        target=GenericReadingTarget(),
        direction=ReadingDirection.IMPORT,
        fetched_at=FETCHED,
        include_quality=False,
    )
    assert len(page.items) == 1

    conflict = _generic_node(value="9")
    with pytest.raises(OejpInvalidResponseError, match="conflicting duplicate"):
        parse_generic_readings_page(
            _generic_payload([duplicate, conflict]),
            supply_point=_point(),
            external_identifier="spin-id",
            target=GenericReadingTarget(),
            direction=ReadingDirection.IMPORT,
            fetched_at=FETCHED,
            include_quality=False,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"supplyPoint": None}),
        lambda payload: payload["supplyPoint"].update({"externalIdentifier": "other"}),
        lambda payload: payload["supplyPoint"].update({"readings": None}),
        lambda payload: payload["supplyPoint"]["readings"].update({"importReadings": None}),
        lambda payload: payload["supplyPoint"]["readings"]["importReadings"].update(
            {"pageInfo": {"hasNextPage": "yes", "endCursor": None}}
        ),
        lambda payload: payload["supplyPoint"]["readings"]["importReadings"]["edges"][0][
            "node"
        ].update({"units": "WATT"}),
        lambda payload: payload["supplyPoint"]["readings"]["importReadings"]["edges"][0][
            "node"
        ].update({"value": "NaN"}),
        lambda payload: payload["supplyPoint"]["readings"]["importReadings"]["edges"][0][
            "node"
        ].update({"intervalStart": "2026-07-01T00:00:00"}),
    ],
)
def test_generic_parser_rejects_unavailable_or_malformed_contract(mutation: object) -> None:
    payload = _generic_payload([_generic_node()])
    mutation(payload)  # type: ignore[operator]

    with pytest.raises((OejpGenericProviderUnavailableError, OejpInvalidResponseError)):
        parse_generic_readings_page(
            payload,
            supply_point=_point(),
            external_identifier="spin-id",
            target=GenericReadingTarget(),
            direction=ReadingDirection.IMPORT,
            fetched_at=FETCHED,
            include_quality=False,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda node: node.pop("intervalStart"),
        lambda node: node.update({"intervalStart": "not-a-date"}),
        lambda node: node.pop("value"),
        lambda node: node.update({"value": None}),
        lambda node: node.update({"value": {}}),
        lambda node: node.update({"value": "not-a-number"}),
        lambda node: node.update({"units": None}),
    ],
)
def test_generic_parser_rejects_malformed_reading_scalars(mutation: object) -> None:
    node = _generic_node()
    mutation(node)  # type: ignore[operator]
    with pytest.raises(OejpInvalidResponseError):
        parse_generic_readings_page(
            _generic_payload([node]),
            supply_point=_point(),
            external_identifier="spin-id",
            target=GenericReadingTarget(),
            direction=ReadingDirection.IMPORT,
            fetched_at=FETCHED,
            include_quality=False,
        )


def test_generic_parser_rejects_malformed_connection_and_identifiers() -> None:
    malformed_edges = _generic_payload([_generic_node()])
    malformed_edges["supplyPoint"]["readings"]["importReadings"]["edges"] = None  # type: ignore[index]
    with pytest.raises(OejpInvalidResponseError, match="edges"):
        parse_generic_readings_page(
            malformed_edges,
            supply_point=_point(),
            external_identifier="spin-id",
            target=GenericReadingTarget(),
            direction=ReadingDirection.IMPORT,
            fetched_at=FETCHED,
            include_quality=False,
        )

    missing_identifier = _generic_payload([_generic_node()])
    missing_identifier["supplyPoint"]["externalIdentifier"] = ""  # type: ignore[index]
    with pytest.raises(OejpInvalidResponseError, match="externalIdentifier"):
        parse_generic_readings_page(
            missing_identifier,
            supply_point=_point(),
            external_identifier="spin-id",
            target=GenericReadingTarget(),
            direction=ReadingDirection.IMPORT,
            fetched_at=FETCHED,
            include_quality=False,
        )

    missing_quality_code = _generic_payload(
        [_generic_node(qualities=[{"quality": "", "value": None, "count": None}])]
    )
    with pytest.raises(OejpInvalidResponseError, match="quality"):
        parse_generic_readings_page(
            missing_quality_code,
            supply_point=_point(),
            external_identifier="spin-id",
            target=GenericReadingTarget(),
            direction=ReadingDirection.IMPORT,
            fetched_at=FETCHED,
            include_quality=True,
        )


@pytest.mark.parametrize(
    "count",
    [-1, True, "1"],
)
def test_generic_parser_rejects_malformed_quality_count(count: object) -> None:
    with pytest.raises(OejpInvalidResponseError, match="quality count"):
        parse_generic_readings_page(
            _generic_payload(
                [_generic_node(qualities=[{"quality": "ACTUAL", "value": "1", "count": count}])]
            ),
            supply_point=_point(),
            external_identifier="spin-id",
            target=GenericReadingTarget(),
            direction=ReadingDirection.IMPORT,
            fetched_at=FETCHED,
            include_quality=True,
        )


def test_generic_parser_rejects_missing_or_duplicate_filtered_series() -> None:
    target = GenericReadingTarget(device_id="device-1")
    missing = _generic_payload([_generic_node()], target=target)
    missing["supplyPoint"]["devices"]["edges"][0]["node"]["deviceIdentifier"] = "other"  # type: ignore[index]
    with pytest.raises(OejpGenericProviderUnavailableError):
        parse_generic_readings_page(
            missing,
            supply_point=_point(),
            external_identifier="spin-id",
            target=target,
            direction=ReadingDirection.IMPORT,
            fetched_at=FETCHED,
            include_quality=False,
        )

    duplicate = _generic_payload([_generic_node()], target=target)
    edges = duplicate["supplyPoint"]["devices"]["edges"]  # type: ignore[index]
    edges.append(deepcopy(edges[0]))
    with pytest.raises(OejpInvalidResponseError, match="filter returned duplicates"):
        parse_generic_readings_page(
            duplicate,
            supply_point=_point(),
            external_identifier="spin-id",
            target=target,
            direction=ReadingDirection.IMPORT,
            fetched_at=FETCHED,
            include_quality=False,
        )


def test_legacy_parsers_preserve_revision_cost_and_separate_sources() -> None:
    point = _point()
    half = parse_legacy_half_hourly_readings(
        _legacy_payload(
            half_hourly=[
                {
                    "startAt": "2026-07-01T09:00:00+09:00",
                    "endAt": "2026-07-01T09:30:00+09:00",
                    "value": "0.25",
                    "costEstimate": "8.75",
                    "version": "LATEST",
                }
            ]
        ),
        supply_point=point,
        fetched_at=FETCHED,
    )
    interval = parse_legacy_interval_readings(
        _legacy_payload(
            interval=[
                {
                    "id": "interval-1",
                    "startAt": "2026-06-01T00:00:00+09:00",
                    "endAt": "2026-07-01T00:00:00+09:00",
                    "value": "120.5",
                    "costEstimate": "4200",
                }
            ]
        ),
        supply_point=point,
        fetched_at=FETCHED,
    )

    assert half[0].version == "LATEST"
    assert half[0].official_cost == Decimal("8.75")
    assert half[0].granularity is ReadingGranularity.THIRTY_MIN
    assert half[0].direction is ReadingDirection.IMPORT
    assert interval[0].source is ReadingSource.LEGACY_INTERVAL
    assert interval[0].granularity is None
    assert interval[0].official_cost == Decimal("4200")


def test_legacy_parsers_allow_unavailable_revision_and_cost() -> None:
    half = parse_legacy_half_hourly_readings(
        _legacy_payload(
            half_hourly=[
                {
                    "startAt": "2026-07-01T00:00:00Z",
                    "endAt": "2026-07-01T00:30:00Z",
                    "value": "0.25",
                    "costEstimate": None,
                    "version": None,
                }
            ]
        ),
        supply_point=_point(),
        fetched_at=FETCHED,
    )
    interval = parse_legacy_interval_readings(
        _legacy_payload(
            interval=[
                {
                    "startAt": "2026-06-01T00:00:00Z",
                    "endAt": "2026-07-01T00:00:00Z",
                    "value": "120.5",
                }
            ]
        ),
        supply_point=_point(),
        fetched_at=FETCHED,
    )

    assert half[0].version is None
    assert half[0].official_cost is None
    assert interval[0].official_cost is None


async def test_legacy_provider_queries_enabled_families_with_bounded_window() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.side_effect = [
        _legacy_payload(half_hourly=[]),
        _legacy_payload(interval=[]),
    ]
    provider = LegacyHalfHourlyProvider(
        client,
        _capabilities(),
        now=lambda: FETCHED,
    )

    assert await provider.async_get_readings(_point(), START, END) == ()
    assert client.execute.await_count == 2
    assert client.execute.await_args_list[0].args[1] == {
        "accountNumber": "account-id",
        "fromDatetime": "2026-07-01T00:00:00Z",
        "toDatetime": "2026-07-01T01:00:00Z",
    }
    assert client.execute.await_args_list[1].args[1]["startAt"] == "2026-07-01T00:00:00Z"


@pytest.mark.parametrize(
    ("half", "interval", "response"),
    [
        (
            CapabilityAvailability.UNSUPPORTED,
            CapabilityAvailability.SUPPORTED,
            _legacy_payload(interval=[]),
        ),
        (
            CapabilityAvailability.SUPPORTED,
            CapabilityAvailability.FORBIDDEN,
            _legacy_payload(half_hourly=[]),
        ),
    ],
)
async def test_legacy_provider_skips_unavailable_families(
    half: CapabilityAvailability,
    interval: CapabilityAvailability,
    response: dict[str, object],
) -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = response
    await LegacyHalfHourlyProvider(
        client,
        _capabilities(half_hourly=half, interval=interval),
        now=lambda: FETCHED,
    ).async_get_readings(_point(), START, END)
    assert client.execute.await_count == 1


async def test_legacy_provider_rejects_when_no_legacy_family_is_available() -> None:
    with pytest.raises(
        OejpNoReadingProviderError,
        match="No legacy",
    ):
        await LegacyHalfHourlyProvider(
            AsyncMock(spec=AuthenticatedGraphQLClient),
            _capabilities(
                half_hourly=CapabilityAvailability.UNSUPPORTED,
                interval=CapabilityAvailability.FORBIDDEN,
            ),
            now=lambda: FETCHED,
        ).async_get_readings(_point(), START, END)


def test_legacy_parser_rejects_wrong_missing_or_duplicate_supply_point() -> None:
    payload = _legacy_payload(half_hourly=[])
    payload["account"]["number"] = "other"  # type: ignore[index]
    with pytest.raises(OejpInvalidResponseError, match="different account"):
        parse_legacy_half_hourly_readings(
            payload,
            supply_point=_point(),
            fetched_at=FETCHED,
        )

    missing = _legacy_payload(half_hourly=[])
    missing["account"]["properties"][0]["electricitySupplyPoints"] = []  # type: ignore[index]
    with pytest.raises(OejpInvalidResponseError, match="requested supply point"):
        parse_legacy_half_hourly_readings(
            missing,
            supply_point=_point(),
            fetched_at=FETCHED,
        )

    duplicate = _legacy_payload(half_hourly=[])
    points = duplicate["account"]["properties"][0]["electricitySupplyPoints"]  # type: ignore[index]
    points.append(deepcopy(points[0]))
    with pytest.raises(OejpInvalidResponseError, match="duplicate requested"):
        parse_legacy_half_hourly_readings(
            duplicate,
            supply_point=_point(),
            fetched_at=FETCHED,
        )


def test_legacy_parser_preserves_explicit_export_direction() -> None:
    reading = parse_legacy_half_hourly_readings(
        _legacy_payload(
            half_hourly=[
                {
                    "startAt": "2026-07-01T00:00:00Z",
                    "endAt": "2026-07-01T00:30:00Z",
                    "value": "1",
                    "costEstimate": "2",
                    "version": 2,
                }
            ]
        ),
        supply_point=_point(direction=ReadingDirection.EXPORT),
        fetched_at=FETCHED,
    )[0]
    assert reading.direction is ReadingDirection.EXPORT


@pytest.mark.parametrize("version", [True, ""])
def test_legacy_parser_rejects_malformed_revision(version: object) -> None:
    with pytest.raises(OejpInvalidResponseError, match="version"):
        parse_legacy_half_hourly_readings(
            _legacy_payload(
                half_hourly=[
                    {
                        "startAt": "2026-07-01T00:00:00Z",
                        "endAt": "2026-07-01T00:30:00Z",
                        "value": "1",
                        "costEstimate": "2",
                        "version": version,
                    }
                ]
            ),
            supply_point=_point(),
            fetched_at=FETCHED,
        )


def test_legacy_parser_matches_id_without_optional_spin() -> None:
    point = OejpSupplyPoint(
        id="supply-id",
        account_number="account-id",
    )
    assert (
        parse_legacy_half_hourly_readings(
            _legacy_payload(half_hourly=[]),
            supply_point=point,
            fetched_at=FETCHED,
        )
        == ()
    )


@pytest.mark.parametrize(
    ("target", "direction"),
    [
        ("unknown", ReadingDirection.IMPORT),
        ("supply_point", ReadingDirection.UNKNOWN),
    ],
)
def test_generic_query_rejects_unknown_target_or_direction(
    target: str,
    direction: ReadingDirection,
) -> None:
    with pytest.raises(ValueError):
        build_generic_readings_query(target, direction, False)


class _ProviderStub:
    def __init__(
        self,
        result: tuple = (),
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def async_get_readings(
        self,
        _supply_point: OejpSupplyPoint,
        _start_at: datetime,
        _end_at: datetime,
    ) -> tuple:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize(
    ("availability", "reason"),
    [
        (
            CapabilityAvailability.UNSUPPORTED,
            ReadingFallbackReason.GENERIC_CAPABILITY_UNSUPPORTED,
        ),
        (
            CapabilityAvailability.FORBIDDEN,
            ReadingFallbackReason.GENERIC_CAPABILITY_FORBIDDEN,
        ),
    ],
)
async def test_router_falls_back_from_observed_capability_gap(
    availability: CapabilityAvailability,
    reason: ReadingFallbackReason,
) -> None:
    generic = _ProviderStub()
    legacy = _ProviderStub()
    router = ReadingProviderRouter(
        generic,
        legacy,
        _capabilities(generic=availability),
    )

    batch = await router.async_get_readings(_point(), START, END)

    assert batch.provider is ReadingProviderName.LEGACY
    assert batch.fallback_reason is reason
    assert {series.source for series in batch.authoritative_series} == {
        ReadingSource.LEGACY_HALF_HOURLY,
        ReadingSource.LEGACY_INTERVAL,
    }
    assert batch.authoritative_sources == frozenset(
        {
            ReadingSource.SUPPLY_POINT_READINGS,
            ReadingSource.LEGACY_HALF_HOURLY,
            ReadingSource.LEGACY_INTERVAL,
        }
    )
    assert generic.calls == 0
    assert legacy.calls == 1


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            OejpAuthorizationError(
                (
                    GraphQLErrorDetail(
                        "safe",
                        error_type="AUTHORIZATION",
                        path=("supplyPoint", "readings"),
                    ),
                )
            ),
            ReadingFallbackReason.GENERIC_PERMISSION_GAP,
        ),
        (
            OejpGenericProviderUnavailableError(GenericUnavailableReason.SERIES_NOT_CONFIGURED),
            ReadingFallbackReason.GENERIC_SUPPLY_POINT_UNAVAILABLE,
        ),
        (
            OejpQueryValidationError((GraphQLErrorDetail("safe", error_code="KT-CT-1113"),)),
            ReadingFallbackReason.GENERIC_FIELD_DISABLED,
        ),
    ],
)
async def test_router_falls_back_only_for_recognized_runtime_gaps(
    error: Exception,
    reason: ReadingFallbackReason,
) -> None:
    batch = await ReadingProviderRouter(
        _ProviderStub(error=error),
        _ProviderStub(),
        _capabilities(),
    ).async_get_readings(_point(), START, END)

    assert batch.provider is ReadingProviderName.LEGACY
    assert batch.fallback_reason is reason


@pytest.mark.parametrize(
    "error",
    [
        OejpAuthenticationError((GraphQLErrorDetail("safe", error_type="AUTHENTICATION"),)),
        OejpRateLimitError((GraphQLErrorDetail("safe", error_code="KT-CT-1199"),)),
        OejpTimeoutError("timeout"),
        OejpTransportError("server"),
        OejpInvalidResponseError("malformed"),
        OejpNotFoundError((GraphQLErrorDetail("safe", error_type="NOT_FOUND"),)),
        OejpQueryValidationError((GraphQLErrorDetail("safe", error_type="VALIDATION"),)),
        OejpAuthorizationError((GraphQLErrorDetail("safe", error_type="AUTHORIZATION"),)),
        OejpAuthorizationError(
            (
                GraphQLErrorDetail(
                    "safe",
                    error_type="AUTHORIZATION",
                    error_code="KT-CT-4177",
                    path=("supplyPoint", "readings"),
                ),
            )
        ),
        OejpAuthorizationError(
            (
                GraphQLErrorDetail(
                    "safe",
                    error_type="AUTHORIZATION",
                    error_code="KT-CT-1112",
                    path=("supplyPoint", "readings"),
                ),
            )
        ),
    ],
)
async def test_router_never_masks_non_fallback_failures(error: Exception) -> None:
    legacy = _ProviderStub()
    with pytest.raises(type(error)):
        await ReadingProviderRouter(
            _ProviderStub(error=error),
            legacy,
            _capabilities(),
        ).async_get_readings(_point(), START, END)
    assert legacy.calls == 0


async def test_router_reports_generic_selection_without_fallback() -> None:
    generic = _ProviderStub()
    legacy = _ProviderStub()

    batch = await ReadingProviderRouter(
        generic,
        legacy,
        _capabilities(),
    ).async_get_readings(_point(), START, END)

    assert batch.readings == ()
    assert batch.provider is ReadingProviderName.GENERIC
    assert batch.fallback_reason is None
    assert len(batch.authoritative_series) == len(EnergyUnit)
    assert {series.unit for series in batch.authoritative_series} == set(EnergyUnit)
    assert {series.source for series in batch.authoritative_series} == {
        ReadingSource.SUPPLY_POINT_READINGS
    }
    assert batch.authoritative_sources == frozenset(
        {
            ReadingSource.SUPPLY_POINT_READINGS,
        }
    )
    assert generic.calls == 1
    assert legacy.calls == 0


async def test_generic_null_supply_point_is_not_a_fallback_signal() -> None:
    client = AsyncMock(spec=AuthenticatedGraphQLClient)
    client.execute.return_value = {"supplyPoint": None}
    generic = GenericReadingsProvider(client, _capabilities(), now=lambda: FETCHED)
    legacy = _ProviderStub()

    with pytest.raises(OejpInvalidResponseError):
        await ReadingProviderRouter(
            generic,
            legacy,
            _capabilities(),
        ).async_get_readings(_point(), START, END)
    assert legacy.calls == 0
