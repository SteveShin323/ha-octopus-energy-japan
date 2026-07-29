"""Tests for API-neutral OEJP domain models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from custom_components.octopus_energy_japan.api import (
    Capability,
    CapabilityAvailability,
    CapabilitySnapshot,
    CapabilityStatus,
    EnergyReading,
    EnergyUnit,
    ReadingDirection,
    ReadingSource,
)


def test_capability_snapshot_replaces_selected_observations() -> None:
    assert (
        CapabilitySnapshot().availability(Capability.DEVICES)
        is CapabilityAvailability.UNKNOWN
    )
    snapshot = CapabilitySnapshot(
        (
            CapabilityStatus(
                Capability.DEVICES,
                CapabilityAvailability.SUPPORTED,
            ),
            CapabilityStatus(
                Capability.GENERIC_READINGS,
                CapabilityAvailability.SUPPORTED,
            ),
        )
    )

    replaced = snapshot.replace(
        (Capability.DEVICES, Capability.REGISTERS),
        CapabilityAvailability.FORBIDDEN,
        "permission",
    )

    assert replaced.availability(Capability.DEVICES) is CapabilityAvailability.FORBIDDEN
    assert replaced.availability(Capability.REGISTERS) is CapabilityAvailability.FORBIDDEN
    assert replaced.availability(Capability.GENERIC_READINGS) is CapabilityAvailability.SUPPORTED


BASE_TIME = datetime(2026, 7, 29, 0, 0, tzinfo=UTC)


def _reading(*, start: datetime, end: datetime) -> EnergyReading:
    return EnergyReading(
        supply_point_id="supply-point",
        direction=ReadingDirection.IMPORT,
        start_at=start,
        end_at=end,
        value=Decimal("0.5"),
        unit=EnergyUnit.KWH,
        source=ReadingSource.LEGACY_HALF_HOURLY,
    )


def test_energy_reading_accepts_timezone_aware_ordered_interval() -> None:
    start = datetime(2026, 7, 29, 0, 0, tzinfo=UTC)

    reading = _reading(start=start, end=start + timedelta(minutes=30))

    assert reading.value == Decimal("0.5")


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (
            BASE_TIME.replace(tzinfo=None),
            datetime(2026, 7, 29, 0, 30, tzinfo=UTC),
            "timezone-aware",
        ),
        (
            datetime(2026, 7, 29, 0, 0, tzinfo=UTC),
            (BASE_TIME + timedelta(minutes=30)).replace(tzinfo=None),
            "timezone-aware",
        ),
        (
            datetime(2026, 7, 29, 0, 30, tzinfo=UTC),
            datetime(2026, 7, 29, 0, 0, tzinfo=UTC),
            "later than",
        ),
        (
            datetime(2026, 7, 29, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 29, 0, 0, tzinfo=UTC),
            "later than",
        ),
    ],
)
def test_energy_reading_rejects_invalid_intervals(
    start: datetime,
    end: datetime,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _reading(start=start, end=end)
