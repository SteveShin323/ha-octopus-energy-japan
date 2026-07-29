"""API-neutral domain models for OEJP resources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


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


@dataclass(frozen=True, slots=True)
class OejpSupplyPoint:
    """Electricity supply point associated with an account."""

    id: str
    account_number: str
    status: str | None = None
    direction: ReadingDirection = ReadingDirection.UNKNOWN


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
