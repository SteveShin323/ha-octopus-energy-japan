"""Tests for the Home Assistant external-statistics adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from unittest.mock import Mock

import pytest
from custom_components.octopus_energy_japan.api import (
    EnergyReading,
    EnergyUnit,
    ReadingDirection,
    ReadingSource,
)
from custom_components.octopus_energy_japan.ledger import LedgerRecord
from custom_components.octopus_energy_japan.statistics_runtime import (
    HomeAssistantStatisticsProjector,
)
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

NOW = datetime(2026, 8, 3, 3, tzinfo=UTC)
SECRET = "11" * 32


class _Ledger:
    known_partitions = frozenset({"2026-07", "2026-08"})

    def __init__(self, records: tuple[LedgerRecord, ...]) -> None:
        self.records = records
        self.requested: tuple[datetime, datetime] | None = None

    async def async_records(
        self,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[LedgerRecord, ...]:
        self.requested = (start_at, end_at)
        return self.records


def _record(*, value: str = "0.5", cost: str | None = None) -> LedgerRecord:
    return LedgerRecord(
        EnergyReading(
            account_id="A-1",
            supply_point_id="SP-1",
            direction=ReadingDirection.IMPORT,
            start_at=datetime(2026, 8, 3, 0, tzinfo=UTC),
            end_at=datetime(2026, 8, 3, 0, 30, tzinfo=UTC),
            value=Decimal(value),
            unit=EnergyUnit.KWH,
            source=ReadingSource.SUPPLY_POINT_READINGS,
            official_cost=Decimal(cost) if cost is not None else None,
            fetched_at=NOW,
        )
    )


async def test_publishes_safe_energy_metadata_and_dirty_rows(hass: HomeAssistant) -> None:
    publisher = Mock()
    ledger = _Ledger((_record(),))
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=publisher,
    )

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=NOW - timedelta(hours=3),
    )

    assert ledger.requested == (datetime(2026, 7, 1, tzinfo=UTC), NOW)
    publisher.assert_called_once()
    metadata = publisher.call_args.args[1]
    statistics = publisher.call_args.args[2]
    assert metadata["source"] == "octopus_energy_japan"
    assert metadata["statistic_id"].startswith("octopus_energy_japan:sp_")
    assert "A-1" not in str(metadata)
    assert "SP-1" not in str(metadata)
    assert metadata["unit_class"] == "energy"
    assert metadata["unit_of_measurement"] == "kWh"
    assert statistics[0]["state"] == 0.5
    assert statistics[0]["sum"] == 0.5


async def test_official_cost_requires_explicit_activation(hass: HomeAssistant) -> None:
    default_publisher = Mock()
    enabled_publisher = Mock()
    ledger = _Ledger((_record(cost="15"),))

    await HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=default_publisher,
    ).async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    await HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        include_official_cost=True,
        publisher=enabled_publisher,
    ).async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    assert default_publisher.call_count == 1
    assert enabled_publisher.call_count == 2
    cost_metadata = enabled_publisher.call_args_list[1].args[1]
    assert cost_metadata["unit_of_measurement"] == "JPY"
    assert cost_metadata["unit_class"] is None


async def test_empty_ledger_is_a_noop(hass: HomeAssistant) -> None:
    publisher = Mock()
    ledger = _Ledger(())
    ledger.known_partitions = frozenset()

    await HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=publisher,
    ).async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    publisher.assert_not_called()


async def test_empty_projection_rows_are_not_published(hass: HomeAssistant) -> None:
    publisher = Mock()
    ledger = _Ledger((_record(),))

    await HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=publisher,
    ).async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=NOW + timedelta(hours=1),
    )

    publisher.assert_not_called()


async def test_empty_partition_index_can_still_clear_deleted_series(
    hass: HomeAssistant,
) -> None:
    projector = HomeAssistantStatisticsProjector(hass, SECRET)
    projector._clear_directions = Mock()  # type: ignore[method-assign]
    ledger = _Ledger(())
    ledger.known_partitions = frozenset()

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=NOW,
        reset_directions=frozenset({ReadingDirection.EXPORT}),
    )

    projector._clear_directions.assert_called_once_with(  # type: ignore[attr-defined]
        "A-1",
        "SP-1",
        frozenset({ReadingDirection.EXPORT}),
    )


def test_clear_with_no_directions_is_a_noop(hass: HomeAssistant) -> None:
    projector = HomeAssistantStatisticsProjector(hass, SECRET)

    projector._clear_directions("A-1", "SP-1", frozenset())


@pytest.mark.recorder_harness
async def test_recorder_harness_replaces_external_hourly_rows(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    ledger = _Ledger((_record(),))
    projector = HomeAssistantStatisticsProjector(hass, SECRET)

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    await async_recorder_block_till_done(hass)

    metadata = await hass.async_add_executor_job(
        partial(
            get_metadata,
            hass,
            statistic_source="octopus_energy_japan",
        )
    )
    assert len(metadata) == 1
    statistic_id = next(iter(metadata))
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 8, 3, tzinfo=UTC),
        NOW,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )

    assert rows[statistic_id][0]["state"] == 0.5
    assert rows[statistic_id][0]["sum"] == 0.5

    ledger.records = (_record(value="0.7"),)
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=datetime(2026, 8, 3, tzinfo=UTC),
    )
    await async_recorder_block_till_done(hass)

    corrected = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 8, 3, tzinfo=UTC),
        NOW,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    assert len(corrected[statistic_id]) == 1
    assert corrected[statistic_id][0]["state"] == 0.7
    assert corrected[statistic_id][0]["sum"] == 0.7

    ledger.records = ()
    await HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        include_official_cost=True,
    ).async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=datetime(2026, 8, 3, tzinfo=UTC),
        reset_directions=frozenset({ReadingDirection.IMPORT}),
    )
    await async_recorder_block_till_done(hass)

    assert not await hass.async_add_executor_job(
        partial(
            get_metadata,
            hass,
            statistic_source="octopus_energy_japan",
        )
    )
