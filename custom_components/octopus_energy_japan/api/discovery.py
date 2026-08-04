"""Strict OEJP resource discovery, pagination, and capability detection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .auth import AuthenticatedGraphQLClient
from .errors import (
    OejpAuthorizationError,
    OejpInvalidResponseError,
    classify_graphql_error_details,
)
from .models import (
    ELECTRICITY_MARKET_NAME,
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
          electricitySupplyPoints {
            id
            spin
            status
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

GENERIC_DEVICES_QUERY = """
query SupplyPointDevices(
  $externalIdentifier: String!
  $marketName: String!
) {
  supplyPoint(
    externalIdentifier: $externalIdentifier
    marketName: $marketName
  ) {
    externalIdentifier
    devices {
      edges {
        node {
          deviceIdentifier
          registers {
            edges {
              node {
                registerIdentifier
              }
            }
          }
        }
      }
    }
  }
}
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


def _optional_scalar_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise OejpInvalidResponseError("Meter capacity was malformed")
    return str(value)
