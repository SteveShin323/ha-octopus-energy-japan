"""OEJP generic and legacy reading providers with strict fallback policy."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from .auth import AuthenticatedGraphQLClient
from .discovery import ConnectionPage, async_paginate
from .errors import (
    OejpAuthorizationError,
    OejpError,
    OejpInvalidResponseError,
    OejpQueryValidationError,
)
from .models import (
    Capability,
    CapabilityAvailability,
    CapabilitySnapshot,
    EnergyReading,
    EnergyUnit,
    OejpSupplyPoint,
    ReadingDirection,
    ReadingGranularity,
    ReadingQuality,
    ReadingSeriesKey,
    ReadingSource,
)

GENERIC_PAGE_SIZE = 99
GENERIC_MARKET_NAME = "ELECTRICITY"
GENERIC_READING_TYPE = "INTERVAL"
GENERIC_TIME_GRANULARITY = "THIRTY_MIN"
GENERIC_TIMEZONE = "UTC"
GENERIC_ENERGY_UNITS = ("WATT_HOURS", "KILOWATT_HOURS", "MEGAWATT_HOURS")

LEGACY_HALF_HOURLY_QUERY = """
query LegacyHalfHourlyReadings(
  $accountNumber: String!
  $fromDatetime: DateTime!
  $toDatetime: DateTime!
) {
  account(accountNumber: $accountNumber) {
    number
    properties {
      electricitySupplyPoints {
        id
        spin
        halfHourlyReadings(
          fromDatetime: $fromDatetime
          toDatetime: $toDatetime
        ) {
          startAt
          endAt
          value
          costEstimate
          version
        }
      }
    }
  }
}
"""

LEGACY_INTERVAL_QUERY = """
query LegacyIntervalReadings(
  $accountNumber: String!
  $startAt: DateTime!
  $endAt: DateTime!
) {
  account(accountNumber: $accountNumber) {
    number
    properties {
      electricitySupplyPoints {
        id
        spin
        intervalReadings(
          startAt: $startAt
          endAt: $endAt
          sortOrder: ASC
        ) {
          id
          startAt
          endAt
          value
          costEstimate
        }
      }
    }
  }
}
"""

_DISABLED_GENERIC_CODES = {"KT-CT-1113"}
_DISABLED_GENERIC_TYPES = {
    "DISABLED",
    "DISABLED_FIELD",
    "FEATURE_DISABLED",
    "FIELD_DISABLED",
}


class ReadingProviderName(StrEnum):
    """Stable provider name exposed to runtime diagnostics."""

    GENERIC = "generic"
    LEGACY = "legacy"


class ReadingFallbackReason(StrEnum):
    """Allow-listed reasons for generic-to-legacy fallback."""

    GENERIC_CAPABILITY_UNSUPPORTED = "generic_capability_unsupported"
    GENERIC_CAPABILITY_FORBIDDEN = "generic_capability_forbidden"
    GENERIC_PERMISSION_GAP = "generic_permission_gap"
    GENERIC_FIELD_DISABLED = "generic_field_disabled"
    GENERIC_SUPPLY_POINT_UNAVAILABLE = "generic_supply_point_unavailable"


class GenericUnavailableReason(StrEnum):
    """Recognized generic provider incompatibilities."""

    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    NO_READING_DIRECTION = "no_reading_direction"
    SERIES_NOT_CONFIGURED = "series_not_configured"


class OejpGenericProviderUnavailableError(OejpError):
    """The generic model is unavailable for a recognized compatibility reason."""

    def __init__(self, reason: GenericUnavailableReason) -> None:
        self.reason = reason
        super().__init__(f"Generic reading provider unavailable ({reason.value})")


class OejpNoReadingProviderError(OejpError):
    """No reading family is available under the observed capabilities."""


@dataclass(frozen=True, slots=True)
class DirectionReadingResult:
    """One authoritative direction result and its provider observation."""

    readings: tuple[EnergyReading, ...]
    direction: ReadingDirection
    provider: ReadingProviderName
    observed_at: datetime
    fallback_reason: ReadingFallbackReason | None = None
    authoritative_series: frozenset[ReadingSeriesKey] = frozenset()
    authoritative_sources: frozenset[ReadingSource] = frozenset()


class ReadingProvider(Protocol):
    """API-neutral interval reading provider."""

    async def async_get_readings(
        self,
        supply_point: OejpSupplyPoint,
        direction: ReadingDirection,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[EnergyReading, ...]:
        """Return normalized readings for the requested half-open interval."""


@dataclass(frozen=True, slots=True)
class GenericReadingTarget:
    device_id: str | None = None
    register_id: str | None = None

    @property
    def kind(self) -> str:
        if self.register_id is not None:
            return "register"
        if self.device_id is not None:
            return "device"
        return "supply_point"


class GenericReadingsProvider:
    """Read the current SupplyPointType/Device/Register readings model."""

    def __init__(
        self,
        client: AuthenticatedGraphQLClient,
        capabilities: CapabilitySnapshot,
        *,
        now: Callable[[], datetime] | None = None,
        page_size: int = GENERIC_PAGE_SIZE,
    ) -> None:
        if page_size <= 0 or page_size >= 100:
            raise ValueError("Generic reading page_size must be between 1 and 99")
        self._client = client
        self._capabilities = capabilities
        self._now = now or (lambda: datetime.now(UTC))
        self._page_size = page_size

    async def async_get_readings(
        self,
        supply_point: OejpSupplyPoint,
        direction: ReadingDirection,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[EnergyReading, ...]:
        """Fetch all configured generic energy series for one supply point."""
        start, end = _validated_window(start_at, end_at)
        if (
            self._capabilities.availability(Capability.GENERIC_READINGS)
            is CapabilityAvailability.UNSUPPORTED
        ):
            raise OejpGenericProviderUnavailableError(
                GenericUnavailableReason.CAPABILITY_UNSUPPORTED
            )

        if direction not in self._directions():
            raise OejpGenericProviderUnavailableError(GenericUnavailableReason.NO_READING_DIRECTION)

        external_identifier = supply_point.spin or supply_point.id
        if not external_identifier:
            raise OejpInvalidResponseError("Supply point did not contain a generic identifier")

        fetched_at = _utc_datetime(self._now(), "Provider clock")
        readings: list[EnergyReading] = []
        for target in _generic_targets(supply_point):
            readings.extend(
                await self._async_fetch_series(
                    supply_point,
                    external_identifier,
                    target,
                    direction,
                    start,
                    end,
                    fetched_at,
                )
            )
        return _deduplicate_readings(readings)

    def _directions(self) -> tuple[ReadingDirection, ...]:
        return _generic_directions(self._capabilities)

    async def _async_fetch_series(
        self,
        supply_point: OejpSupplyPoint,
        external_identifier: str,
        target: GenericReadingTarget,
        direction: ReadingDirection,
        start_at: datetime,
        end_at: datetime,
        fetched_at: datetime,
    ) -> tuple[EnergyReading, ...]:
        include_quality = (
            self._capabilities.availability(Capability.READING_QUALITY)
            is CapabilityAvailability.SUPPORTED
        )

        async def fetch_series(with_quality: bool) -> tuple[EnergyReading, ...]:
            query = build_generic_readings_query(target.kind, direction, with_quality)

            async def fetch_page(cursor: str | None) -> ConnectionPage[EnergyReading]:
                variables: dict[str, Any] = {
                    "externalIdentifier": external_identifier,
                    "marketName": GENERIC_MARKET_NAME,
                    "startAt": _graphql_datetime(start_at),
                    "endAt": _graphql_datetime(end_at),
                    "after": cursor,
                    "first": self._page_size,
                    "units": list(GENERIC_ENERGY_UNITS),
                }
                if target.device_id is not None:
                    variables["deviceIdentifier"] = target.device_id
                if target.register_id is not None:
                    variables["registerIdentifier"] = target.register_id
                data = await self._client.execute(query, variables)
                return parse_generic_readings_page(
                    data,
                    supply_point=supply_point,
                    external_identifier=external_identifier,
                    target=target,
                    direction=direction,
                    fetched_at=fetched_at,
                    include_quality=with_quality,
                )

            return await async_paginate(fetch_page)

        try:
            return await fetch_series(include_quality)
        except OejpAuthorizationError as err:
            if not include_quality or not _only_quality_authorization_errors(err):
                raise
            return await fetch_series(False)


class LegacyHalfHourlyProvider:
    """Read legacy half-hour and billing-period interval readings."""

    def __init__(
        self,
        client: AuthenticatedGraphQLClient,
        capabilities: CapabilitySnapshot,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._capabilities = capabilities
        self._now = now or (lambda: datetime.now(UTC))

    async def async_get_readings(
        self,
        supply_point: OejpSupplyPoint,
        direction: ReadingDirection,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[EnergyReading, ...]:
        """Fetch every available legacy reading family for one supply point."""
        start, end = _validated_window(start_at, end_at)
        if direction is not _legacy_direction(supply_point):
            raise OejpNoReadingProviderError(
                "Legacy readings cannot represent the requested direction"
            )
        fetched_at = _utc_datetime(self._now(), "Provider clock")
        readings: list[EnergyReading] = []
        half_availability = self._capabilities.availability(Capability.LEGACY_HALF_HOURLY_READINGS)
        interval_availability = self._capabilities.availability(Capability.LEGACY_INTERVAL_READINGS)

        if half_availability not in {
            CapabilityAvailability.UNSUPPORTED,
            CapabilityAvailability.FORBIDDEN,
        }:
            data = await self._client.execute(
                LEGACY_HALF_HOURLY_QUERY,
                {
                    "accountNumber": supply_point.account_number,
                    "fromDatetime": _graphql_datetime(start),
                    "toDatetime": _graphql_datetime(end),
                },
            )
            readings.extend(
                parse_legacy_half_hourly_readings(
                    data,
                    supply_point=supply_point,
                    fetched_at=fetched_at,
                )
            )

        if interval_availability not in {
            CapabilityAvailability.UNSUPPORTED,
            CapabilityAvailability.FORBIDDEN,
        }:
            data = await self._client.execute(
                LEGACY_INTERVAL_QUERY,
                {
                    "accountNumber": supply_point.account_number,
                    "startAt": _graphql_datetime(start),
                    "endAt": _graphql_datetime(end),
                },
            )
            readings.extend(
                parse_legacy_interval_readings(
                    data,
                    supply_point=supply_point,
                    fetched_at=fetched_at,
                )
            )

        if (
            not readings
            and half_availability
            in {
                CapabilityAvailability.UNSUPPORTED,
                CapabilityAvailability.FORBIDDEN,
            }
            and interval_availability
            in {
                CapabilityAvailability.UNSUPPORTED,
                CapabilityAvailability.FORBIDDEN,
            }
        ):
            raise OejpNoReadingProviderError("No legacy reading capability is available")
        return _deduplicate_readings(readings)


class ReadingProviderRouter:
    """Prefer generic readings and allow only explicit compatibility fallback."""

    def __init__(
        self,
        generic: ReadingProvider,
        legacy: ReadingProvider,
        capabilities: CapabilitySnapshot,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._generic = generic
        self._legacy = legacy
        self._capabilities = capabilities
        self._now = now or (lambda: datetime.now(UTC))

    async def async_get_readings(
        self,
        supply_point: OejpSupplyPoint,
        direction: ReadingDirection,
        start_at: datetime,
        end_at: datetime,
    ) -> DirectionReadingResult:
        """Return one direction result without masking operational failures."""
        if direction not in {ReadingDirection.IMPORT, ReadingDirection.EXPORT}:
            raise ValueError("Reading provider routing requires import or export direction")
        availability = self._capabilities.availability(Capability.GENERIC_READINGS)
        if availability is CapabilityAvailability.UNSUPPORTED:
            return await self._legacy_batch(
                supply_point,
                direction,
                start_at,
                end_at,
                ReadingFallbackReason.GENERIC_CAPABILITY_UNSUPPORTED,
            )
        if availability is CapabilityAvailability.FORBIDDEN:
            return await self._legacy_batch(
                supply_point,
                direction,
                start_at,
                end_at,
                ReadingFallbackReason.GENERIC_CAPABILITY_FORBIDDEN,
            )

        try:
            readings = await self._generic.async_get_readings(
                supply_point,
                direction,
                start_at,
                end_at,
            )
        except OejpAuthorizationError as err:
            if not _is_generic_reading_permission_gap(err):
                raise
            return await self._legacy_batch(
                supply_point,
                direction,
                start_at,
                end_at,
                ReadingFallbackReason.GENERIC_PERMISSION_GAP,
            )
        except OejpGenericProviderUnavailableError as err:
            reason = {
                GenericUnavailableReason.CAPABILITY_UNSUPPORTED: (
                    ReadingFallbackReason.GENERIC_CAPABILITY_UNSUPPORTED
                ),
                GenericUnavailableReason.NO_READING_DIRECTION: (
                    ReadingFallbackReason.GENERIC_CAPABILITY_UNSUPPORTED
                ),
                GenericUnavailableReason.SERIES_NOT_CONFIGURED: (
                    ReadingFallbackReason.GENERIC_SUPPLY_POINT_UNAVAILABLE
                ),
            }[err.reason]
            return await self._legacy_batch(
                supply_point,
                direction,
                start_at,
                end_at,
                reason,
            )
        except OejpQueryValidationError as err:
            if not _is_disabled_generic_field(err):
                raise
            return await self._legacy_batch(
                supply_point,
                direction,
                start_at,
                end_at,
                ReadingFallbackReason.GENERIC_FIELD_DISABLED,
            )
        return DirectionReadingResult(
            readings=readings,
            direction=direction,
            provider=ReadingProviderName.GENERIC,
            observed_at=_result_observed_at(readings, self._now()),
            authoritative_series=_generic_authoritative_series(
                supply_point,
                direction,
            ),
            authoritative_sources=frozenset(
                {
                    ReadingSource.SUPPLY_POINT_READINGS,
                }
            ),
        )

    async def _legacy_batch(
        self,
        supply_point: OejpSupplyPoint,
        direction: ReadingDirection,
        start_at: datetime,
        end_at: datetime,
        reason: ReadingFallbackReason,
    ) -> DirectionReadingResult:
        readings = await self._legacy.async_get_readings(
            supply_point,
            direction,
            start_at,
            end_at,
        )
        authoritative_series = _legacy_authoritative_series(
            supply_point,
            self._capabilities,
            direction,
        )
        return DirectionReadingResult(
            readings=readings,
            direction=direction,
            provider=ReadingProviderName.LEGACY,
            observed_at=_result_observed_at(readings, self._now()),
            fallback_reason=reason,
            authoritative_series=authoritative_series,
            authoritative_sources=frozenset(
                {
                    ReadingSource.SUPPLY_POINT_READINGS,
                    *(series.source for series in authoritative_series),
                }
            ),
        )


def build_generic_readings_query(
    target_kind: str,
    direction: ReadingDirection,
    include_quality: bool,
) -> str:
    """Build one stable, bounded connection operation for a generic series."""
    if target_kind not in {"supply_point", "device", "register"}:
        raise ValueError("Unsupported generic reading target")
    if direction not in {ReadingDirection.IMPORT, ReadingDirection.EXPORT}:
        raise ValueError("Generic readings require import or export direction")

    direction_field = "importReadings" if direction is ReadingDirection.IMPORT else "exportReadings"
    operation_suffix = "Import" if direction is ReadingDirection.IMPORT else "Export"
    quality_fields = (
        "\n                  qualities { quality value count }" if include_quality else ""
    )
    reading_selection = f"""
                {direction_field}(first: $first, after: $after) {{
                  pageInfo {{ hasNextPage endCursor }}
                  edges {{
                    node {{
                      intervalStart
                      intervalEnd
                      value
                      units{quality_fields}
                    }}
                  }}
                }}"""
    readings_selection = f"""
              readings(
                startAt: $startAt
                endAt: $endAt
                readingType: INTERVAL
                timeGranularity: THIRTY_MIN
                timezone: "UTC"
                units: $units
              ) {{{reading_selection}
              }}"""

    common_variables = """
  $externalIdentifier: String!
  $marketName: String!
  $startAt: DateTime!
  $endAt: DateTime!
  $units: [Units!]
  $first: Int!
  $after: String"""
    if target_kind == "supply_point":
        return f"""
query SupplyPoint{operation_suffix}Readings({common_variables}
) {{
  supplyPoint(
    externalIdentifier: $externalIdentifier
    marketName: $marketName
  ) {{
    externalIdentifier{readings_selection}
  }}
}}
"""

    common_variables += "\n  $deviceIdentifier: String!"
    if target_kind == "device":
        return f"""
query Device{operation_suffix}Readings({common_variables}
) {{
  supplyPoint(
    externalIdentifier: $externalIdentifier
    marketName: $marketName
  ) {{
    externalIdentifier
    devices(deviceIdentifiers: [$deviceIdentifier], first: 2) {{
      edges {{
        node {{
          deviceIdentifier{readings_selection}
        }}
      }}
    }}
  }}
}}
"""

    common_variables += "\n  $registerIdentifier: String!"
    return f"""
query Register{operation_suffix}Readings({common_variables}
) {{
  supplyPoint(
    externalIdentifier: $externalIdentifier
    marketName: $marketName
  ) {{
    externalIdentifier
    devices(deviceIdentifiers: [$deviceIdentifier], first: 2) {{
      edges {{
        node {{
          deviceIdentifier
          registers(registerIdentifiers: [$registerIdentifier], first: 2) {{
            edges {{
              node {{
                registerIdentifier{readings_selection}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def parse_generic_readings_page(
    data: Mapping[str, Any],
    *,
    supply_point: OejpSupplyPoint,
    external_identifier: str,
    target: GenericReadingTarget,
    direction: ReadingDirection,
    fetched_at: datetime,
    include_quality: bool,
) -> ConnectionPage[EnergyReading]:
    """Parse one generic direction page and validate its selected series."""
    point = _required_mapping(
        data.get("supplyPoint"),
        "Generic readings did not contain supplyPoint",
    )
    returned_identifier = _required_string(
        point,
        "externalIdentifier",
        "Generic readings supply point",
    )
    if returned_identifier != external_identifier:
        raise OejpInvalidResponseError("Generic readings returned a different supply point")

    container = point
    if target.device_id is not None:
        container = _find_single_filtered_node(
            point.get("devices"),
            identifier_key="deviceIdentifier",
            expected=target.device_id,
            context="device",
        )
    if target.register_id is not None:
        container = _find_single_filtered_node(
            container.get("registers"),
            identifier_key="registerIdentifier",
            expected=target.register_id,
            context="register",
        )

    readings = _required_mapping(
        container.get("readings"),
        "Generic reading series was unavailable",
        unavailable=GenericUnavailableReason.SERIES_NOT_CONFIGURED,
    )
    field = "importReadings" if direction is ReadingDirection.IMPORT else "exportReadings"
    connection = _required_mapping(
        readings.get(field),
        f"Generic {field} connection was missing",
    )
    page_info = _required_mapping(
        connection.get("pageInfo"),
        f"Generic {field} pageInfo was missing",
    )
    has_next_page = page_info.get("hasNextPage")
    if not isinstance(has_next_page, bool):
        raise OejpInvalidResponseError(f"Generic {field} hasNextPage was malformed")
    end_cursor = _optional_string(page_info.get("endCursor"))
    edges = _required_list(
        connection.get("edges"),
        f"Generic {field} edges were missing",
    )
    parsed: list[EnergyReading] = []
    for edge in edges:
        raw_edge = _required_mapping(
            edge,
            f"Generic {field} contained a malformed edge",
        )
        raw = _required_mapping(
            raw_edge.get("node"),
            f"Generic {field} edge did not contain a reading",
        )
        parsed.append(
            _parse_generic_reading(
                raw,
                supply_point=supply_point,
                target=target,
                direction=direction,
                fetched_at=fetched_at,
                include_quality=include_quality,
            )
        )
    return ConnectionPage(
        _deduplicate_readings(parsed),
        has_next_page,
        end_cursor,
    )


def parse_legacy_half_hourly_readings(
    data: Mapping[str, Any],
    *,
    supply_point: OejpSupplyPoint,
    fetched_at: datetime,
) -> tuple[EnergyReading, ...]:
    """Parse the exact requested legacy supply point's half-hour readings."""
    raw_point = _legacy_supply_point(data, supply_point)
    raw_readings = _required_list(
        raw_point.get("halfHourlyReadings"),
        "Legacy supply point did not contain halfHourlyReadings",
    )
    direction = _legacy_direction(supply_point)
    readings = [
        EnergyReading(
            account_id=supply_point.account_number,
            supply_point_id=supply_point.id,
            direction=direction,
            start_at=_required_datetime(raw, "startAt", "Legacy half-hour reading"),
            end_at=_required_datetime(raw, "endAt", "Legacy half-hour reading"),
            value=_required_decimal(raw, "value", "Legacy half-hour reading"),
            unit=EnergyUnit.KWH,
            source=ReadingSource.LEGACY_HALF_HOURLY,
            granularity=ReadingGranularity.THIRTY_MIN,
            version=_optional_scalar_string(
                raw.get("version"),
                "Legacy half-hour reading version",
            ),
            official_cost=_optional_decimal(
                raw.get("costEstimate"),
                "Legacy half-hour reading costEstimate",
            ),
            fetched_at=fetched_at,
        )
        for raw in _mapping_items(raw_readings, "Legacy half-hour reading")
    ]
    return _deduplicate_readings(readings)


def parse_legacy_interval_readings(
    data: Mapping[str, Any],
    *,
    supply_point: OejpSupplyPoint,
    fetched_at: datetime,
) -> tuple[EnergyReading, ...]:
    """Parse legacy billing-period interval readings as a separate source."""
    raw_point = _legacy_supply_point(data, supply_point)
    raw_readings = _required_list(
        raw_point.get("intervalReadings"),
        "Legacy supply point did not contain intervalReadings",
    )
    direction = _legacy_direction(supply_point)
    readings = [
        EnergyReading(
            account_id=supply_point.account_number,
            supply_point_id=supply_point.id,
            direction=direction,
            start_at=_required_datetime(raw, "startAt", "Legacy interval reading"),
            end_at=_required_datetime(raw, "endAt", "Legacy interval reading"),
            value=_required_decimal(raw, "value", "Legacy interval reading"),
            unit=EnergyUnit.KWH,
            source=ReadingSource.LEGACY_INTERVAL,
            official_cost=_optional_decimal(
                raw.get("costEstimate"),
                "Legacy interval reading costEstimate",
            ),
            fetched_at=fetched_at,
        )
        for raw in _mapping_items(raw_readings, "Legacy interval reading")
    ]
    return _deduplicate_readings(readings)


def _parse_generic_reading(
    raw: Mapping[str, Any],
    *,
    supply_point: OejpSupplyPoint,
    target: GenericReadingTarget,
    direction: ReadingDirection,
    fetched_at: datetime,
    include_quality: bool,
) -> EnergyReading:
    qualities: tuple[ReadingQuality, ...] = ()
    if include_quality:
        raw_qualities = _required_list(
            raw.get("qualities"),
            "Generic reading qualities were missing",
        )
        qualities = tuple(
            sorted(
                (
                    _parse_quality(item)
                    for item in _mapping_items(raw_qualities, "Generic reading quality")
                ),
                key=lambda quality: (
                    quality.code,
                    quality.value if quality.value is not None else Decimal("-Infinity"),
                    quality.count if quality.count is not None else -1,
                ),
            )
        )
    return EnergyReading(
        account_id=supply_point.account_number,
        supply_point_id=supply_point.id,
        device_id=target.device_id,
        register_id=target.register_id,
        direction=direction,
        start_at=_required_datetime(raw, "intervalStart", "Generic reading"),
        end_at=_required_datetime(raw, "intervalEnd", "Generic reading"),
        value=_required_decimal(raw, "value", "Generic reading"),
        unit=_parse_energy_unit(raw.get("units")),
        source=ReadingSource.SUPPLY_POINT_READINGS,
        granularity=ReadingGranularity.THIRTY_MIN,
        qualities=qualities,
        fetched_at=fetched_at,
    )


def _parse_quality(raw: Mapping[str, Any]) -> ReadingQuality:
    code = _required_string(raw, "quality", "Generic reading quality")
    value = _optional_decimal(raw.get("value"), "Generic reading quality value")
    count = raw.get("count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise OejpInvalidResponseError("Generic reading quality count was malformed")
    return ReadingQuality(code=code, value=value, count=count)


def _generic_targets(supply_point: OejpSupplyPoint) -> tuple[GenericReadingTarget, ...]:
    targets: list[GenericReadingTarget] = []
    for device in supply_point.devices:
        if device.registers:
            targets.extend(
                GenericReadingTarget(device_id=device.id, register_id=register.id)
                for register in device.registers
            )
        else:
            targets.append(GenericReadingTarget(device_id=device.id))
    return tuple(targets) or (GenericReadingTarget(),)


def _generic_directions(
    capabilities: CapabilitySnapshot,
) -> tuple[ReadingDirection, ...]:
    directions: list[ReadingDirection] = []
    if capabilities.availability(Capability.IMPORT_READINGS) is CapabilityAvailability.SUPPORTED:
        directions.append(ReadingDirection.IMPORT)
    if capabilities.availability(Capability.EXPORT_READINGS) is CapabilityAvailability.SUPPORTED:
        directions.append(ReadingDirection.EXPORT)
    return tuple(directions)


def _generic_authoritative_series(
    supply_point: OejpSupplyPoint,
    direction: ReadingDirection,
) -> frozenset[ReadingSeriesKey]:
    return frozenset(
        ReadingSeriesKey(
            account_id=supply_point.account_number,
            supply_point_id=supply_point.id,
            device_id=target.device_id,
            register_id=target.register_id,
            direction=direction,
            unit=unit,
            source=ReadingSource.SUPPLY_POINT_READINGS,
        )
        for target in _generic_targets(supply_point)
        for unit in EnergyUnit
    )


def _legacy_authoritative_series(
    supply_point: OejpSupplyPoint,
    capabilities: CapabilitySnapshot,
    direction: ReadingDirection,
) -> frozenset[ReadingSeriesKey]:
    excluded = {
        CapabilityAvailability.UNSUPPORTED,
        CapabilityAvailability.FORBIDDEN,
    }
    source_capabilities = (
        (
            ReadingSource.LEGACY_HALF_HOURLY,
            Capability.LEGACY_HALF_HOURLY_READINGS,
        ),
        (
            ReadingSource.LEGACY_INTERVAL,
            Capability.LEGACY_INTERVAL_READINGS,
        ),
    )
    return frozenset(
        ReadingSeriesKey(
            account_id=supply_point.account_number,
            supply_point_id=supply_point.id,
            direction=direction,
            unit=EnergyUnit.KWH,
            source=source,
        )
        for source, capability in source_capabilities
        if capabilities.availability(capability) not in excluded
    )


def _legacy_supply_point(
    data: Mapping[str, Any],
    supply_point: OejpSupplyPoint,
) -> Mapping[str, Any]:
    account = _required_mapping(
        data.get("account"),
        "Legacy readings did not contain account",
    )
    returned_account = _required_string(account, "number", "Legacy readings account")
    if returned_account != supply_point.account_number:
        raise OejpInvalidResponseError("Legacy readings returned a different account")
    properties = _required_list(
        account.get("properties"),
        "Legacy readings did not contain properties",
    )
    matches: list[Mapping[str, Any]] = []
    for property_ in _mapping_items(properties, "Legacy reading property"):
        points = _required_list(
            property_.get("electricitySupplyPoints"),
            "Legacy reading property did not contain electricitySupplyPoints",
        )
        for point in _mapping_items(points, "Legacy reading supply point"):
            identifiers = {
                value
                for value in (
                    _optional_string(point.get("id")),
                    _optional_string(point.get("spin")),
                )
                if value is not None
            }
            expected = {supply_point.id}
            if supply_point.spin:
                expected.add(supply_point.spin)
            if identifiers & expected:
                matches.append(point)
    if not matches:
        raise OejpInvalidResponseError("Legacy readings did not contain the requested supply point")
    if len(matches) != 1:
        raise OejpInvalidResponseError(
            "Legacy readings contained duplicate requested supply points"
        )
    return matches[0]


def _find_single_filtered_node(
    value: object,
    *,
    identifier_key: str,
    expected: str,
    context: str,
) -> Mapping[str, Any]:
    connection = _required_mapping(
        value,
        f"Generic {context} connection was missing",
        unavailable=GenericUnavailableReason.SERIES_NOT_CONFIGURED,
    )
    edges = _required_list(
        connection.get("edges"),
        f"Generic {context} connection did not contain edges",
    )
    matches: list[Mapping[str, Any]] = []
    for edge in edges:
        raw_edge = _required_mapping(edge, f"Generic {context} edge was malformed")
        node = _required_mapping(
            raw_edge.get("node"),
            f"Generic {context} edge did not contain node",
        )
        if _optional_string(node.get(identifier_key)) == expected:
            matches.append(node)
    if not matches:
        raise OejpGenericProviderUnavailableError(GenericUnavailableReason.SERIES_NOT_CONFIGURED)
    if len(matches) != 1:
        raise OejpInvalidResponseError(f"Generic {context} filter returned duplicates")
    return matches[0]


def _deduplicate_readings(readings: list[EnergyReading]) -> tuple[EnergyReading, ...]:
    by_interval: dict[
        tuple[ReadingSeriesKey, datetime, datetime],
        EnergyReading,
    ] = {}
    for reading in readings:
        key = (
            ReadingSeriesKey.from_reading(reading),
            reading.start_at,
            reading.end_at,
        )
        existing = by_interval.get(key)
        if existing is not None and existing != reading:
            raise OejpInvalidResponseError(
                "Reading response contained a conflicting duplicate interval"
            )
        by_interval[key] = reading
    return tuple(
        by_interval[key]
        for key in sorted(
            by_interval,
            key=lambda item: (
                item[0].account_id,
                item[0].supply_point_id,
                item[0].device_id or "",
                item[0].register_id or "",
                item[0].direction.value,
                item[0].unit.value,
                item[0].source.value,
                item[1],
                item[2],
            ),
        )
    )


def _result_observed_at(
    readings: tuple[EnergyReading, ...],
    fallback: datetime,
) -> datetime:
    observations = {
        reading.fetched_at.astimezone(UTC) for reading in readings if reading.fetched_at is not None
    }
    if any(reading.fetched_at is None for reading in readings):
        raise OejpInvalidResponseError(
            "Reading provider returned a reading without an observation time"
        )
    if len(observations) > 1:
        raise OejpInvalidResponseError("Reading provider returned multiple observation times")
    return next(iter(observations), _utc_datetime(fallback, "Provider router clock"))


def _is_disabled_generic_field(error: OejpQueryValidationError) -> bool:
    return any(
        detail.error_code in _DISABLED_GENERIC_CODES
        or _normalized_marker(detail.error_type) in _DISABLED_GENERIC_TYPES
        for detail in error.details
    )


def _only_quality_authorization_errors(error: OejpAuthorizationError) -> bool:
    return bool(error.details) and all(
        detail.path and "qualities" in detail.path for detail in error.details
    )


def _is_generic_reading_permission_gap(error: OejpAuthorizationError) -> bool:
    """Allow fallback only for permission failures scoped below supplyPoint."""
    disallowed_codes = {"KT-CT-1112", "KT-CT-4177"}
    generic_fields = {
        "devices",
        "registers",
        "readings",
        "importReadings",
        "exportReadings",
    }
    return bool(error.details) and all(
        detail.error_code not in disallowed_codes and bool(generic_fields.intersection(detail.path))
        for detail in error.details
    )


def _legacy_direction(supply_point: OejpSupplyPoint) -> ReadingDirection:
    if supply_point.direction is not ReadingDirection.UNKNOWN:
        return supply_point.direction
    return ReadingDirection.IMPORT


def candidate_directions(
    supply_point: OejpSupplyPoint,
    capabilities: CapabilitySnapshot,
    previously_queryable: tuple[ReadingDirection, ...] = (),
) -> tuple[ReadingDirection, ...]:
    """Return deterministic probe candidates without implying entity support."""
    directions = {
        direction
        for direction in previously_queryable
        if direction in {ReadingDirection.IMPORT, ReadingDirection.EXPORT}
    }
    if supply_point.direction in {ReadingDirection.IMPORT, ReadingDirection.EXPORT}:
        directions.add(supply_point.direction)
    if capabilities.availability(Capability.IMPORT_READINGS) is CapabilityAvailability.SUPPORTED:
        directions.add(ReadingDirection.IMPORT)
    if capabilities.availability(Capability.EXPORT_READINGS) is CapabilityAvailability.SUPPORTED:
        directions.add(ReadingDirection.EXPORT)

    legacy_unavailable = {
        CapabilityAvailability.UNSUPPORTED,
        CapabilityAvailability.FORBIDDEN,
    }
    legacy_may_work = any(
        capabilities.availability(capability) not in legacy_unavailable
        for capability in (
            Capability.LEGACY_HALF_HOURLY_READINGS,
            Capability.LEGACY_INTERVAL_READINGS,
        )
    )
    if supply_point.direction is ReadingDirection.UNKNOWN and legacy_may_work:
        directions.add(ReadingDirection.IMPORT)
    return tuple(sorted(directions, key=lambda item: item.value))


def _validated_window(start_at: datetime, end_at: datetime) -> tuple[datetime, datetime]:
    start = _utc_datetime(start_at, "Reading start")
    end = _utc_datetime(end_at, "Reading end")
    if end <= start:
        raise ValueError("Reading end must be later than start")
    return start, end


def _utc_datetime(value: datetime, context: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{context} must be timezone-aware")
    return value.astimezone(UTC)


def _graphql_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _required_datetime(
    payload: Mapping[str, Any],
    key: str,
    context: str,
) -> datetime:
    value = payload.get(key)
    if not isinstance(value, str):
        raise OejpInvalidResponseError(f"{context} was missing {key}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise OejpInvalidResponseError(f"{context} contained malformed {key}") from err
    if parsed.tzinfo is None:
        raise OejpInvalidResponseError(f"{context} contained timezone-naive {key}")
    return parsed.astimezone(UTC)


def _required_decimal(
    payload: Mapping[str, Any],
    key: str,
    context: str,
) -> Decimal:
    if key not in payload:
        raise OejpInvalidResponseError(f"{context} was missing {key}")
    value = _optional_decimal(payload[key], f"{context} {key}")
    if value is None:
        raise OejpInvalidResponseError(f"{context} contained null {key}")
    return value


def _optional_decimal(value: object, context: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise OejpInvalidResponseError(f"{context} was malformed")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as err:
        raise OejpInvalidResponseError(f"{context} was malformed") from err
    if not parsed.is_finite():
        raise OejpInvalidResponseError(f"{context} was not finite")
    return parsed


def _parse_energy_unit(value: object) -> EnergyUnit:
    if not isinstance(value, str):
        raise OejpInvalidResponseError("Generic reading units were malformed")
    normalized = value.strip().replace("-", "_").replace(" ", "_").upper()
    aliases = {
        "WH": EnergyUnit.WH,
        "WATT_HOUR": EnergyUnit.WH,
        "WATT_HOURS": EnergyUnit.WH,
        "KWH": EnergyUnit.KWH,
        "KILOWATT_HOUR": EnergyUnit.KWH,
        "KILOWATT_HOURS": EnergyUnit.KWH,
        "MWH": EnergyUnit.MWH,
        "MEGAWATT_HOUR": EnergyUnit.MWH,
        "MEGAWATT_HOURS": EnergyUnit.MWH,
    }
    try:
        return aliases[normalized]
    except KeyError as err:
        raise OejpInvalidResponseError("Generic reading used an unsupported energy unit") from err


def _mapping_items(
    values: list[Any],
    context: str,
) -> tuple[Mapping[str, Any], ...]:
    return tuple(_required_mapping(value, f"{context} was malformed") for value in values)


def _required_mapping(
    value: object,
    message: str,
    *,
    unavailable: GenericUnavailableReason | None = None,
) -> Mapping[str, Any]:
    if value is None and unavailable is not None:
        raise OejpGenericProviderUnavailableError(unavailable)
    if not isinstance(value, Mapping):
        raise OejpInvalidResponseError(message)
    return value


def _required_list(value: object, message: str) -> list[Any]:
    if not isinstance(value, list):
        raise OejpInvalidResponseError(message)
    return value


def _required_string(
    payload: Mapping[str, Any],
    key: str,
    context: str,
) -> str:
    value = _optional_string(payload.get(key))
    if value is None:
        raise OejpInvalidResponseError(f"{context} was missing {key}")
    return value


def _optional_scalar_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise OejpInvalidResponseError(f"{context} was malformed")
    normalized = str(value).strip()
    if not normalized:
        raise OejpInvalidResponseError(f"{context} was malformed")
    return normalized


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalized_marker(value: str | None) -> str | None:
    return value.strip().replace("-", "_").upper() if value else None
