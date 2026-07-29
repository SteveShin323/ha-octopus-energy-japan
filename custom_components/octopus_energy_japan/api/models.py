"""API-neutral domain models for OEJP resources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class ResourceLifecycle(StrEnum):
    """Normalized lifecycle independent of provider status spelling."""

    ACTIVE = "active"
    HISTORICAL = "historical"
    UNKNOWN = "unknown"


class Capability(StrEnum):
    """Optional OEJP features detected for an authenticated viewer."""

    LEGACY_HALF_HOURLY_READINGS = "legacy_half_hourly_readings"
    LEGACY_INTERVAL_READINGS = "legacy_interval_readings"
    GENERIC_READINGS = "generic_readings"
    DEVICES = "devices"
    REGISTERS = "registers"
    IMPORT_READINGS = "import_readings"
    EXPORT_READINGS = "export_readings"


class CapabilityAvailability(StrEnum):
    """Availability of one optional API capability."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    FORBIDDEN = "forbidden"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    """One capability observation with a safe diagnostic reason."""

    capability: Capability
    availability: CapabilityAvailability
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Immutable capability registry for a discovery refresh."""

    statuses: tuple[CapabilityStatus, ...] = ()

    def availability(self, capability: Capability) -> CapabilityAvailability:
        """Return availability, defaulting to unknown when not probed."""
        for status in self.statuses:
            if status.capability is capability:
                return status.availability
        return CapabilityAvailability.UNKNOWN

    def replace(
        self,
        capabilities: tuple[Capability, ...],
        availability: CapabilityAvailability,
        reason: str,
    ) -> CapabilitySnapshot:
        """Replace selected observations while preserving deterministic order."""
        selected = set(capabilities)
        by_capability = {status.capability: status for status in self.statuses}
        for capability in selected:
            by_capability[capability] = CapabilityStatus(
                capability,
                availability,
                reason,
            )
        return CapabilitySnapshot(
            tuple(
                by_capability[capability]
                for capability in Capability
                if capability in by_capability
            )
        )


@dataclass(frozen=True, slots=True)
class OejpRegister:
    """Meter/device register exposed by a generic supply point."""

    id: str
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class OejpDevice:
    """Physical or logical device associated with a supply point."""

    id: str
    device_type: str | None = None
    registers: tuple[OejpRegister, ...] = ()


@dataclass(frozen=True, slots=True)
class OejpMeter:
    """Legacy electricity meter associated with a supply point."""

    serial_number: str
    capacity: str | None = None


@dataclass(frozen=True, slots=True)
class OejpProperty:
    """Property container without address or customer display data."""

    id: str
    supply_points: tuple[OejpSupplyPoint, ...] = ()


class ReadingDirection(StrEnum):
    """Energy flow direction."""

    IMPORT = "import"
    EXPORT = "export"
    UNKNOWN = "unknown"


class EnergyUnit(StrEnum):
    """Supported energy units."""

    KWH = "kWh"
    WH = "Wh"


class ReadingSource(StrEnum):
    """Originating OEJP API family."""

    LEGACY_HALF_HOURLY = "legacy_half_hourly"
    SUPPLY_POINT_READINGS = "supply_point_readings"


@dataclass(frozen=True, slots=True)
class OejpAccount:
    """OEJP customer account discovered for an authenticated viewer."""

    number: str
    status: str | None = None
    lifecycle: ResourceLifecycle = ResourceLifecycle.UNKNOWN
    properties: tuple[OejpProperty, ...] = ()


@dataclass(frozen=True, slots=True)
class OejpSupplyPoint:
    """Electricity supply point associated with an account."""

    id: str
    account_number: str
    status: str | None = None
    direction: ReadingDirection = ReadingDirection.UNKNOWN
    lifecycle: ResourceLifecycle = ResourceLifecycle.UNKNOWN
    property_id: str | None = None
    spin: str | None = None
    meters: tuple[OejpMeter, ...] = ()
    devices: tuple[OejpDevice, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadingQuality:
    """Optional quality metadata supplied by OEJP."""

    code: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class EnergyReading:
    """Normalized interval reading independent of GraphQL response generation."""

    supply_point_id: str
    direction: ReadingDirection
    start_at: datetime
    end_at: datetime
    value: Decimal
    unit: EnergyUnit
    source: ReadingSource
    version: str | None = None
    quality: ReadingQuality | None = None
    cost_estimate: Decimal | None = None
    fetched_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("Reading timestamps must be timezone-aware")
        if self.end_at <= self.start_at:
            raise ValueError("Reading end_at must be later than start_at")
