"""Strict OEJP resource discovery, pagination, and capability detection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .auth import AuthenticatedGraphQLClient
from .errors import (
    OejpAuthorizationError,
    OejpInvalidResponseError,
    classify_graphql_error_details,
)
from .models import (
    ELECTRICITY_MARKET_NAME,
    MAX_PAGE_SIZE,
    Capability,
    CapabilityAvailability,
    CapabilitySnapshot,
    CapabilityStatus,
    OejpAccount,
    OejpDevice,
    OejpMeter,
    OejpProperty,
    OejpRegister,
    OejpSupplyPoint,
    ResourceLifecycle,
)

LEGACY_DISCOVERY_QUERY = """
query ViewerResourceDiscovery {
  viewer {
    accounts {
      number
      status
      ... on Account {
        properties {
          id
          address
          postcode
          electricitySupplyPoints {
            id
            spin
            status
            readingDateDayOfMonth
            nextReadingDate
            nextNextReadingDate
            meters {
              serialNumber
              capacity
            }
          }
        }
      }
    }
  }
}
"""

# Asked account-scoped rather than added to the document above, because authorization for this
# field depends on the path. Measured on a real account: through `account(accountNumber:)` it
# returns data, and through `viewer.accounts` it returns AUTHORIZATION/KT-CT-4501 and nulls the
# field, which a strict discovery turns into a failed setup.
#
# `supplyStartAt` anchors the billing period the tariff accumulates over. It is optional
# throughout: without it the cost formula falls back to the calendar month, which is what it
# used before this was requested.
ACCOUNT_SUPPLY_PERIODS_QUERY = """
query AccountSupplyPeriods($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    number
    properties {
      electricitySupplyPoints {
        id
        spin
        supplyPeriods {
          supplyStartAt
          supplyEndAt
          isBillable
        }
      }
    }
  }
}
"""

CAPABILITY_QUERY = """
query OejpSchemaCapabilities {
  queryType: __type(name: "Query") { fields { name } }
  supplyPointType: __type(name: "SupplyPointType") { fields { name } }
  deviceType: __type(name: "Device") { fields { name } }
  readingsType: __type(name: "Readings") { fields { name } }
  readingType: __type(name: "Reading") { fields { name } }
  legacySupplyPointType: __type(name: "ElectricitySupplyPoint") { fields { name } }
}
"""

GENERIC_DEVICES_QUERY = f"""
query SupplyPointDevices(
  $externalIdentifier: String!
  $marketName: String!
) {{
  supplyPoint(
    externalIdentifier: $externalIdentifier
    marketName: $marketName
  ) {{
    externalIdentifier
    devices(first: {MAX_PAGE_SIZE}) {{
      edges {{
        node {{
          deviceIdentifier
          registers(first: {MAX_PAGE_SIZE}) {{
            edges {{
              node {{
                registerIdentifier
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

_ACTIVE_STATUSES = {"ACTIVE", "OPEN", "ON_SUPPLY", "LIVE", "CURRENT"}
_HISTORICAL_STATUSES = {
    "CLOSED",
    "ENDED",
    "EXPIRED",
    "HISTORICAL",
    "INACTIVE",
    "OFF_SUPPLY",
    "TERMINATED",
}


@dataclass(frozen=True, slots=True)
class ConnectionPage[T]:
    """One validated Relay-style page."""

    items: tuple[T, ...]
    has_next_page: bool
    end_cursor: str | None


async def async_paginate[T](
    fetch_page: Callable[[str | None], Awaitable[ConnectionPage[T]]],
    *,
    max_pages: int = 1_000,
) -> tuple[T, ...]:
    """Collect a forward connection with cursor-loop and page bounds."""
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    cursor: str | None = None
    seen_cursors: set[str] = set()
    items: list[T] = []
    for _page_number in range(max_pages):
        page = await fetch_page(cursor)
        items.extend(page.items)
        if not page.has_next_page:
            return tuple(items)
        if not page.end_cursor:
            raise OejpInvalidResponseError("Connection hasNextPage was true without endCursor")
        if page.end_cursor in seen_cursors:
            raise OejpInvalidResponseError("Connection pagination cursor repeated")
        seen_cursors.add(page.end_cursor)
        cursor = page.end_cursor
    raise OejpInvalidResponseError("Connection exceeded the page safety limit")


async def async_discover_resources(
    client: AuthenticatedGraphQLClient,
) -> tuple[OejpAccount, ...]:
    """Discover all legacy account/property/supply-point/meter resources."""
    data = await client.execute(LEGACY_DISCOVERY_QUERY)
    return parse_legacy_discovery(data)


async def async_discover_supply_starts(
    client: AuthenticatedGraphQLClient,
    account_number: str,
) -> dict[str, datetime]:
    """Return when billable supply began, per supply point, for one account.

    Optional throughout: an account that may not read this gets an empty mapping and the cost
    formula falls back to the calendar month. Nothing else depends on it, so a refusal must not
    reach setup.
    """
    result = await client.execute_optional(
        ACCOUNT_SUPPLY_PERIODS_QUERY,
        {"accountNumber": account_number},
    )
    if result.data is None:
        return {}
    return parse_supply_starts(result.data)


def parse_supply_starts(data: Mapping[str, Any]) -> dict[str, datetime]:
    """Parse supply starts keyed by both the supply point's id and its spin.

    Both keys, because the discovery tree identifies a supply point by `id` or by `spin`
    depending on which the provider returned, and the caller matches on whichever it holds.
    """
    account = data.get("account")
    if not isinstance(account, Mapping):
        return {}
    starts: dict[str, datetime] = {}
    properties = account.get("properties")
    for property_ in properties if isinstance(properties, list) else []:
        if not isinstance(property_, Mapping):
            continue
        points = property_.get("electricitySupplyPoints")
        for point in points if isinstance(points, list) else []:
            if not isinstance(point, Mapping):
                continue
            start = _supply_start(point.get("supplyPeriods"))
            if start is None:
                continue
            for key in (point.get("id"), point.get("spin")):
                if identifier := _optional_string(key):
                    starts[identifier] = start
    return starts


def attach_supply_starts(
    accounts: tuple[OejpAccount, ...],
    starts_by_supply_point: Mapping[str, datetime],
) -> tuple[OejpAccount, ...]:
    """Return an immutable discovery tree enriched with each supply point's start."""
    enriched: list[OejpAccount] = []
    for account in accounts:
        properties: list[OejpProperty] = []
        for property_ in account.properties:
            supply_points = tuple(
                replace(
                    point,
                    supply_start_at=starts_by_supply_point.get(
                        point.id,
                        starts_by_supply_point.get(point.spin or point.id, point.supply_start_at),
                    ),
                )
                for point in property_.supply_points
            )
            properties.append(replace(property_, supply_points=supply_points))
        enriched.append(replace(account, properties=tuple(properties)))
    return tuple(enriched)


def parse_legacy_discovery(data: Mapping[str, Any]) -> tuple[OejpAccount, ...]:
    """Parse a legacy discovery response into deterministic typed resources."""
    viewer = _required_mapping(data.get("viewer"), "Discovery response was missing viewer")
    raw_accounts = _required_list(
        viewer.get("accounts"),
        "Discovery response did not contain an accounts list",
    )
    accounts: dict[str, OejpAccount] = {}
    for raw_account in raw_accounts:
        account = _parse_account(raw_account)
        _insert_unique(accounts, account.number, account, "account")
    return tuple(accounts[key] for key in sorted(accounts))


async def async_detect_capabilities(
    client: AuthenticatedGraphQLClient,
) -> CapabilitySnapshot:
    """Detect schema features without converting authorization into reauth."""
    result = await client.execute_optional(CAPABILITY_QUERY)
    if result.errors:
        error = classify_graphql_error_details(
            result.errors,
            retry_after=result.retry_after,
        )
        if result.data is None and isinstance(error, OejpAuthorizationError):
            return _uniform_capabilities(
                CapabilityAvailability.FORBIDDEN,
                "schema_introspection_forbidden",
            )
        if result.data is None:
            raise error
    if result.data is None:
        raise OejpInvalidResponseError("Capability response did not contain data")
    return parse_capabilities(
        result.data,
        missing_availability=(
            CapabilityAvailability.FORBIDDEN
            if result.errors
            else CapabilityAvailability.UNSUPPORTED
        ),
    )


async def async_discover_generic_devices(
    client: AuthenticatedGraphQLClient,
    external_identifier: str,
) -> tuple[OejpDevice, ...]:
    """Discover generic devices/registers for one known electricity supply point."""
    data = await client.execute(
        GENERIC_DEVICES_QUERY,
        {
            "externalIdentifier": external_identifier,
            "marketName": ELECTRICITY_MARKET_NAME,
        },
    )
    supply_point = _required_mapping(
        data.get("supplyPoint"),
        "Generic device discovery did not contain supplyPoint",
    )
    returned_identifier = _required_string(
        supply_point,
        "externalIdentifier",
        "Generic supply point",
    )
    if returned_identifier != external_identifier:
        raise OejpInvalidResponseError("Generic device discovery returned a different supply point")
    return parse_generic_devices(supply_point.get("devices"))


def attach_generic_devices(
    accounts: tuple[OejpAccount, ...],
    devices_by_supply_point: Mapping[str, tuple[OejpDevice, ...]],
) -> tuple[OejpAccount, ...]:
    """Return an immutable discovery tree enriched with generic devices."""
    enriched_accounts: list[OejpAccount] = []
    for account in accounts:
        properties: list[OejpProperty] = []
        for property_ in account.properties:
            supply_points = tuple(
                replace(
                    supply_point,
                    devices=devices_by_supply_point.get(
                        supply_point.spin or supply_point.id,
                        supply_point.devices,
                    ),
                )
                for supply_point in property_.supply_points
            )
            properties.append(replace(property_, supply_points=supply_points))
        enriched_accounts.append(replace(account, properties=tuple(properties)))
    return tuple(enriched_accounts)


def parse_capabilities(
    data: Mapping[str, Any],
    *,
    missing_availability: CapabilityAvailability = CapabilityAvailability.UNSUPPORTED,
) -> CapabilitySnapshot:
    """Convert introspection field names into a stable capability registry."""
    query_fields = _optional_field_names(data.get("queryType"))
    supply_fields = _optional_field_names(data.get("supplyPointType"))
    device_fields = _optional_field_names(data.get("deviceType"))
    reading_fields = _optional_field_names(data.get("readingsType"))
    reading_node_fields = _optional_field_names(data.get("readingType"))
    legacy_fields = _optional_field_names(data.get("legacySupplyPointType"))

    observations = {
        Capability.LEGACY_HALF_HOURLY_READINGS: "halfHourlyReadings" in legacy_fields,
        Capability.LEGACY_INTERVAL_READINGS: "intervalReadings" in legacy_fields,
        Capability.GENERIC_READINGS: (
            "supplyPoint" in query_fields and "readings" in supply_fields
        ),
        Capability.DEVICES: ("supplyPoint" in query_fields and "devices" in supply_fields),
        Capability.REGISTERS: (
            "supplyPoint" in query_fields
            and "devices" in supply_fields
            and "registers" in device_fields
        ),
        Capability.IMPORT_READINGS: "importReadings" in reading_fields,
        Capability.EXPORT_READINGS: "exportReadings" in reading_fields,
        Capability.READING_QUALITY: "qualities" in reading_node_fields,
    }
    return CapabilitySnapshot(
        tuple(
            CapabilityStatus(
                capability,
                (CapabilityAvailability.SUPPORTED if supported else missing_availability),
            )
            for capability, supported in observations.items()
        )
    )


def lifecycle_from_status(status: str | None) -> ResourceLifecycle:
    """Normalize provider lifecycle strings without guessing unknown values."""
    if status is None:
        return ResourceLifecycle.UNKNOWN
    normalized = status.strip().replace("-", "_").replace(" ", "_").upper()
    if normalized in _ACTIVE_STATUSES:
        return ResourceLifecycle.ACTIVE
    if normalized in _HISTORICAL_STATUSES:
        return ResourceLifecycle.HISTORICAL
    return ResourceLifecycle.UNKNOWN


def _parse_account(value: object) -> OejpAccount:
    raw = _required_mapping(value, "Discovery response contained a malformed account")
    number = _required_string(raw, "number", "Account")
    status = _optional_string(raw.get("status"))
    raw_properties = _required_list(
        raw.get("properties"),
        "Account discovery did not contain properties",
    )
    properties: dict[str, OejpProperty] = {}
    for raw_property in raw_properties:
        property_ = _parse_property(raw_property, number)
        _insert_unique(properties, property_.id, property_, "property")
    return OejpAccount(
        number=number,
        status=status,
        lifecycle=lifecycle_from_status(status),
        properties=tuple(properties[key] for key in sorted(properties)),
    )


def _parse_property(value: object, account_number: str) -> OejpProperty:
    raw = _required_mapping(value, "Discovery response contained a malformed property")
    property_id = _required_string(raw, "id", "Property")
    raw_points = _required_list(
        raw.get("electricitySupplyPoints"),
        "Property discovery did not contain electricitySupplyPoints",
    )
    points: dict[str, OejpSupplyPoint] = {}
    for raw_point in raw_points:
        point = _parse_supply_point(raw_point, account_number, property_id)
        _insert_unique(points, point.id, point, "supply point")
    return OejpProperty(
        id=property_id,
        supply_points=tuple(points[key] for key in sorted(points)),
        address=_optional_string(raw.get("address")),
        postcode=_optional_string(raw.get("postcode")),
    )


def _parse_supply_point(
    value: object,
    account_number: str,
    property_id: str,
) -> OejpSupplyPoint:
    raw = _required_mapping(
        value,
        "Discovery response contained a malformed supply point",
    )
    provider_id = _optional_string(raw.get("id"))
    spin = _optional_string(raw.get("spin"))
    if provider_id is None and spin is None:
        raise OejpInvalidResponseError("Supply point discovery did not contain an identifier")
    status = _optional_string(raw.get("status"))
    raw_meters = _required_list(
        raw.get("meters"),
        "Supply point discovery did not contain meters",
    )
    meters: dict[str, OejpMeter] = {}
    for raw_meter in raw_meters:
        meter = _parse_meter(raw_meter)
        _insert_unique(meters, meter.serial_number, meter, "meter")
    return OejpSupplyPoint(
        id=provider_id or spin or "",
        account_number=account_number,
        status=status,
        lifecycle=lifecycle_from_status(status),
        property_id=property_id,
        spin=spin,
        reading_day_of_month=_reading_day(raw.get("readingDateDayOfMonth")),
        reading_schedule_day=_reading_schedule_day(
            raw.get("nextReadingDate"),
            raw.get("nextNextReadingDate"),
        ),
        meters=tuple(meters[key] for key in sorted(meters)),
    )


def _parse_meter(value: object) -> OejpMeter:
    raw = _required_mapping(value, "Discovery response contained a malformed meter")
    return OejpMeter(
        serial_number=_required_string(raw, "serialNumber", "Meter"),
        capacity=_optional_scalar_string(raw.get("capacity")),
    )


def parse_generic_devices(value: object) -> tuple[OejpDevice, ...]:
    """Parse a generic devices connection, including register connections."""
    devices: dict[str, OejpDevice] = {}
    for raw_device in _connection_nodes(value, "devices"):
        device = _parse_device(raw_device)
        _insert_unique(devices, device.id, device, "device")
    return tuple(devices[key] for key in sorted(devices))


def _parse_device(value: object) -> OejpDevice:
    raw = _required_mapping(value, "Device connection contained a malformed node")
    device_id = _required_identifier(raw, ("deviceIdentifier", "id"), "Device")
    registers: dict[str, OejpRegister] = {}
    for raw_register in _connection_nodes(raw.get("registers"), "registers"):
        register_raw = _required_mapping(
            raw_register,
            "Register connection contained a malformed node",
        )
        register = OejpRegister(
            id=_required_identifier(
                register_raw,
                ("registerIdentifier", "id"),
                "Register",
            ),
            unit=_optional_string(register_raw.get("unit")),
        )
        _insert_unique(registers, register.id, register, "register")
    return OejpDevice(
        id=device_id,
        device_type=_optional_string(raw.get("deviceType")),
        registers=tuple(registers[key] for key in sorted(registers)),
    )


def _connection_nodes(value: object, context: str) -> tuple[object, ...]:
    connection = _required_mapping(value, f"{context} connection was missing")
    edges = _required_list(
        connection.get("edges"),
        f"{context} connection did not contain edges",
    )
    nodes: list[object] = []
    for edge in edges:
        raw_edge = _required_mapping(edge, f"{context} connection contained a malformed edge")
        if "node" not in raw_edge:
            raise OejpInvalidResponseError(f"{context} connection edge was missing node")
        nodes.append(raw_edge["node"])
    return tuple(nodes)


def _optional_field_names(value: object) -> frozenset[str]:
    if value is None:
        return frozenset()
    raw_type = _required_mapping(value, "Capability type was malformed")
    raw_fields = _required_list(raw_type.get("fields"), "Capability fields were malformed")
    fields: set[str] = set()
    for raw_field in raw_fields:
        field = _required_mapping(raw_field, "Capability field was malformed")
        fields.add(_required_string(field, "name", "Capability field"))
    return frozenset(fields)


def _uniform_capabilities(
    availability: CapabilityAvailability,
    reason: str,
) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        tuple(CapabilityStatus(capability, availability, reason) for capability in Capability)
    )


def _insert_unique[T](
    target: dict[str, T],
    key: str,
    value: T,
    context: str,
) -> None:
    existing = target.get(key)
    if existing is not None and existing != value:
        raise OejpInvalidResponseError(
            f"Discovery response contained conflicting duplicate {context}"
        )
    target[key] = value


def _required_mapping(value: object, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OejpInvalidResponseError(message)
    return value


def _required_list(value: object, message: str) -> list[Any]:
    if not isinstance(value, list):
        raise OejpInvalidResponseError(message)
    return value


def _required_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = _optional_string(payload.get(key))
    if value is None:
        raise OejpInvalidResponseError(f"{context} was missing {key}")
    return value


def _required_identifier(
    payload: Mapping[str, Any],
    keys: tuple[str, ...],
    context: str,
) -> str:
    for key in keys:
        if value := _optional_string(payload.get(key)):
            return value
    raise OejpInvalidResponseError(f"{context} was missing {' or '.join(keys)}")


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _reading_day(value: object) -> int | None:
    """Return the meter-reading day of the month, or None if it is not a usable day.

    A value outside 1 to 31 is dropped rather than published. It would be a schema change or a
    provider fault, and a "reading day" of 0 or 40 is worse than no reading day: an
    automation would act on it.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 31 else None


def _reading_schedule_day(first: object, second: object) -> int | None:
    """Return the day of the month the meter is scheduled to be read on, when corroborated.

    Two consecutive scheduled dates that fall on the same day one month apart are the recurring
    schedule stated twice. That is closer evidence for the billing anchor than the day supply
    began, which lands on the read day only if service happened to start on one.

    Both dates are required to agree. A pair a different number of months apart describes a
    schedule this calendar does not model, and a pair whose days differ — which a month too
    short to hold the day would produce — says nothing certain, so both fall back.

    The dates themselves are deliberately not published anywhere: measured on a real account
    they were a stale snapshot, both already in the past. A day of the month is stable under
    that staleness in a way a date is not.
    """
    one = _optional_datetime(first)
    two = _optional_datetime(second)
    if one is None or two is None or one.day != two.day:
        return None
    if (two.year - one.year) * 12 + (two.month - one.month) != 1:
        return None
    return one.day


def _supply_start(value: object) -> datetime | None:
    """Return when billable supply began, from the earliest billable supply period.

    Lenient throughout: this anchors the billing period the tariff accumulates over, and the
    fallback when it is unknown is the calendar month, which is what the code did before this
    field existed. Raising would fail discovery over a field consumption does not need.

    Only billable periods count. A non-billable one is a gap the customer is not charged for,
    so it cannot be where a charging period starts.
    """
    if not isinstance(value, list):
        return None
    starts = [
        parsed
        for period in value
        if isinstance(period, Mapping)
        and period.get("isBillable") is True
        and (parsed := _optional_datetime(period.get("supplyStartAt"))) is not None
    ]
    return min(starts) if starts else None


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _optional_scalar_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise OejpInvalidResponseError("Meter capacity was malformed")
    return str(value)
