"""Tests for the Home Assistant external-statistics adapter."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from custom_components.octopus_energy_japan.api import (
    EnergyReading,
    EnergyUnit,
    ReadingDirection,
    ReadingSource,
)
from custom_components.octopus_energy_japan.billing_period import BillingPeriodCalendar
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
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

NOW = datetime(2026, 8, 3, 3, tzinfo=UTC)
SECRET = "11" * 32
# Statistics need the recorder, which `after_dependencies` orders but does not require,
# so tests that project have to declare it. Its absence has its own test below.
RECORDER = "recorder"
TOKYO = ZoneInfo("Asia/Tokyo")


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


def _record(
    *,
    value: str = "0.5",
    cost: str | None = None,
    direction: ReadingDirection = ReadingDirection.IMPORT,
) -> LedgerRecord:
    return LedgerRecord(
        EnergyReading(
            account_id="A-1",
            supply_point_id="SP-1",
            direction=direction,
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
    hass.config.components.add(RECORDER)
    publisher = Mock()
    ledger = _Ledger((_record(),))
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=publisher,
        cleaner=Mock(),
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
    hass.config.components.add(RECORDER)
    default_publisher = Mock()
    enabled_publisher = Mock()
    ledger = _Ledger((_record(cost="15"),))

    await HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=default_publisher,
        cleaner=Mock(),
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
        cleaner=Mock(),
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
    hass.config.components.add(RECORDER)
    publisher = Mock()
    ledger = _Ledger(())
    ledger.known_partitions = frozenset()

    await HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=publisher,
        cleaner=Mock(),
    ).async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    publisher.assert_not_called()


async def test_empty_projection_rows_are_not_published(hass: HomeAssistant) -> None:
    hass.config.components.add(RECORDER)
    publisher = Mock()
    ledger = _Ledger((_record(),))

    await HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=publisher,
        cleaner=Mock(),
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
    hass.config.components.add(RECORDER)
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
    hass.config.components.add(RECORDER)
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


async def test_projection_is_skipped_without_the_recorder(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`after_dependencies` orders the recorder; it does not require it.

    Without `recorder:` in the configuration, `get_instance` raised
    `KeyError: recorder_instance`, observed live on 2026-08-04. Consumption entities and
    calendar totals do not need the recorder, so statistics are skipped rather than
    allowed to fail the refresh.
    """
    hass.config.components.add(RECORDER)
    hass.config.components.remove(RECORDER)
    publisher = Mock()
    ledger = _Ledger((_record(),))
    projector = HomeAssistantStatisticsProjector(hass, SECRET, publisher=publisher)

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
        reset_directions=frozenset({ReadingDirection.IMPORT}),
    )

    publisher.assert_not_called()
    assert ledger.requested is None
    assert "recorder is not enabled" in caplog.text

    # It names a deliberate configuration choice, so a refresh must not repeat it.
    caplog.clear()
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    assert "recorder is not enabled" not in caplog.text


@pytest.mark.recorder_harness
async def test_the_energy_dashboard_accepts_the_published_statistics(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """The Energy dashboard must accept these as grid consumption and return.

    Metadata that merely looks right is not enough: Home Assistant's own energy
    validator decides, and it rejects a statistic that lacks a sum, uses a unit it
    cannot convert, or does not exist. This asserts it accepts both directions with no
    errors and no warnings, which is what "connect it in the Energy menu" depends on.
    """
    from homeassistant.components.energy import data as energy_data
    from homeassistant.components.energy import validate as energy_validate
    from homeassistant.setup import async_setup_component

    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        tariff_lookup=lambda _account, _point: _priceable_tariff(),
    )
    for direction in (ReadingDirection.IMPORT, ReadingDirection.EXPORT):
        await projector.async_project_supply_point(
            _Ledger((_record(direction=direction),)),  # type: ignore[arg-type]
            "A-1",
            "SP-1",
            NOW,
            dirty_from=None,
        )
    await async_recorder_block_till_done(hass)

    published = await hass.async_add_executor_job(
        partial(get_metadata, hass, statistic_source="octopus_energy_japan")
    )
    # Import energy, export energy, and the cost derived from the import.
    assert len(published) == 3
    consumption = next(key for key in published if key.endswith("_import_energy"))
    ret = next(key for key in published if key.endswith("_export_energy"))
    cost = next(key for key in published if key.endswith("_tariff_cost"))

    assert await async_setup_component(hass, "energy", {})
    manager = await energy_data.async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": consumption,
                    "stat_energy_to": ret,
                    # The whole point of publishing a cost statistic: the dashboard cannot
                    # price an external statistic itself, so this is the only way the cost
                    # column can be filled.
                    "stat_cost": cost,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "stat_compensation": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0.0,
                }
            ]
        }
    )

    result = await energy_validate.async_validate(hass)

    assert len(result.energy_sources) == 1
    # Empty issues means the validator accepted both statistics as they are.
    assert result.energy_sources[0].issues == {}, result.energy_sources[0].issues
    assert result.device_consumption == [], result.device_consumption

    # And the metadata is what makes that possible, so pin the parts it checks.
    _, cost_metadata = published[cost]
    assert cost_metadata["has_sum"] is True
    assert cost_metadata["unit_of_measurement"] == "JPY"
    for statistic_id in (consumption, ret):
        _, metadata = published[statistic_id]
        assert metadata["has_sum"] is True
        assert metadata["unit_of_measurement"] == "kWh"
        assert metadata["source"] == "octopus_energy_japan"
        assert statistic_id.startswith("octopus_energy_japan:")


@pytest.mark.recorder_harness
async def test_the_statistic_name_is_the_one_shown_in_the_energy_picker(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """The Energy dashboard shows this name and nothing else.

    An identity digest there is unreadable — a household with two supply points cannot
    tell which is which — so the name follows the supply-point device, which is already
    labelled with a per-account ordinal and carries no provider identifier.
    """
    from custom_components.octopus_energy_japan.const import DOMAIN
    from custom_components.octopus_energy_japan.identity import (
        stable_supply_point_identity,
    )
    from homeassistant.helpers import device_registry as dr

    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    identity = stable_supply_point_identity(SECRET, "A-1", "SP-1")
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, identity)},
        name="OEJP supply point 1-1",
    )

    await HomeAssistantStatisticsProjector(hass, SECRET).async_project_supply_point(
        _Ledger((_record(),)),  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    await async_recorder_block_till_done(hass)

    published = await hass.async_add_executor_job(
        partial(get_metadata, hass, statistic_source=DOMAIN)
    )
    name = next(iter(published.values()))[1]["name"]

    assert name == "OEJP supply point 1-1 Import energy"
    # The digest must not appear: it is what made the old name unreadable.
    assert identity.rsplit("-", maxsplit=1)[-1][:8] not in name
    assert "A-1" not in name
    assert "SP-1" not in name


@pytest.mark.recorder_harness
async def test_a_statistic_published_before_its_device_still_avoids_identifiers(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """Setup creates devices first, but a race must not leak a provider identifier."""
    from custom_components.octopus_energy_japan.const import DOMAIN

    await HomeAssistantStatisticsProjector(hass, SECRET).async_project_supply_point(
        _Ledger((_record(),)),  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    await async_recorder_block_till_done(hass)

    published = await hass.async_add_executor_job(
        partial(get_metadata, hass, statistic_source=DOMAIN)
    )
    name = next(iter(published.values()))[1]["name"]

    assert name.startswith("OEJP ")
    assert "A-1" not in name
    assert "SP-1" not in name


def test_the_energy_dashboard_cannot_price_an_external_statistic() -> None:
    """Documented behaviour that decides how cost could ever be shown.

    `homeassistant/components/energy/sensor.py` builds a cost sensor only when the
    energy source is a valid entity id, and returns early otherwise. An external
    statistic id is not one, so a price typed into the Energy dashboard is ignored rather
    than applied — which means a cost statistic published by this integration is the only
    possible route. The guides say so; this pins the fact they rely on.
    """
    from custom_components.octopus_energy_japan.api import ReadingDirection
    from custom_components.octopus_energy_japan.statistics import StatisticKind
    from custom_components.octopus_energy_japan.statistics_runtime import statistic_id_for
    from homeassistant.core import valid_entity_id

    statistic_id = statistic_id_for(
        "supply-point-" + "ab" * 32,
        ReadingDirection.IMPORT,
        StatisticKind.ENERGY,
    )

    assert ":" in statistic_id
    assert not valid_entity_id(statistic_id)


def _priceable_tariff(step_price: str = "20.62") -> object:
    from custom_components.octopus_energy_japan.api.tariff import (
        SupplyPointTariff,
        TariffAdder,
        TariffStep,
    )

    return SupplyPointTariff(
        account_number="A-1",
        supply_point_id="SP-1",
        product_code="P",
        product_name="P",
        steps=(
            TariffStep(
                start_kwh=Decimal(0), end_kwh=Decimal(120), price_inc_tax=Decimal(step_price)
            ),
            TariffStep(start_kwh=Decimal(120), end_kwh=None, price_inc_tax=Decimal("25.29")),
        ),
        standing_charge_per_day=Decimal("38.80"),
        # Real adders carry the period they apply to; one without a start cannot be
        # archived, because there is no period to file it under.
        fuel_cost_adjustment=TariffAdder(
            price_inc_tax=Decimal("4.32"),
            valid_from=datetime(2026, 7, 31, 15, tzinfo=UTC),
            valid_to=datetime(2026, 8, 31, 15, tzinfo=UTC),
        ),
        renewable_energy_levy=TariffAdder(
            price_inc_tax=Decimal("4.18"),
            valid_from=datetime(2026, 4, 30, 15, tzinfo=UTC),
            valid_to=datetime(2027, 4, 30, 15, tzinfo=UTC),
        ),
    )


async def test_a_cost_series_is_published_when_a_tariff_is_known(hass: HomeAssistant) -> None:
    """The Energy dashboard cannot price an external statistic, so this is the only route.

    `homeassistant/components/energy/sensor.py` builds a cost sensor only for a real entity
    id, so a price typed into the dashboard is ignored. A cost statistic published here is
    what fills `stat_cost`.
    """
    hass.config.components.add(RECORDER)
    published: list[tuple[object, ...]] = []
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
        tariff_lookup=lambda _account, _point: _priceable_tariff(),
    )

    await projector.async_project_supply_point(
        _Ledger((_record(),)),  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    kinds = [args[1]["statistic_id"] for args in published]
    assert any(statistic_id.endswith("_import_energy") for statistic_id in kinds)
    cost_id = next(statistic_id for statistic_id in kinds if statistic_id.endswith("_tariff_cost"))
    cost_metadata = next(args[1] for args in published if args[1]["statistic_id"] == cost_id)
    assert cost_metadata["unit_of_measurement"] == "JPY"
    assert cost_metadata["has_sum"] is True
    assert cost_metadata["unit_class"] is None
    assert cost_metadata["name"].endswith("Import cost")

    rows = next(args[2] for args in published if args[1]["statistic_id"] == cost_id)
    # 0.5 kWh at the first step, plus both adders, plus one hour of the standing charge.
    expected = (
        Decimal("0.5") * Decimal("20.62")
        + Decimal("0.5") * Decimal("8.50")
        + Decimal("38.80") / Decimal(24)
    )
    assert rows[0]["state"] == pytest.approx(float(expected))
    assert rows[0]["sum"] == pytest.approx(float(expected))


async def test_no_cost_series_without_a_tariff(hass: HomeAssistant) -> None:
    hass.config.components.add(RECORDER)
    published: list[tuple[object, ...]] = []
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
        tariff_lookup=lambda _account, _point: None,
    )

    await projector.async_project_supply_point(
        _Ledger((_record(),)),  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    assert published
    assert not any(args[1]["statistic_id"].endswith("_tariff_cost") for args in published)


async def test_no_cost_series_when_the_tariff_carries_no_steps(hass: HomeAssistant) -> None:
    """A flat-rate product has no `consumptionCharges`; inventing one would be a guess."""
    from custom_components.octopus_energy_japan.api.tariff import SupplyPointTariff

    hass.config.components.add(RECORDER)
    published: list[tuple[object, ...]] = []
    unpriceable = SupplyPointTariff(
        account_number="A-1",
        supply_point_id="SP-1",
        product_code="P",
        product_name="P",
        steps=(),
        standing_charge_per_day=Decimal("38.80"),
        fuel_cost_adjustment=None,
        renewable_energy_levy=None,
    )
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
        tariff_lookup=lambda _account, _point: unpriceable,
    )

    await projector.async_project_supply_point(
        _Ledger((_record(),)),  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    assert not any(args[1]["statistic_id"].endswith("_tariff_cost") for args in published)


async def test_the_cost_sum_continues_across_a_correction(hass: HomeAssistant) -> None:
    """`dirty_from` limits the rows returned, never the sum they carry.

    A correction has to rewrite the affected hour and every later cumulative total, so the
    sum is accumulated from the whole ledger and only the tail is published.
    """
    hass.config.components.add(RECORDER)
    published: list[tuple[object, ...]] = []
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
        tariff_lookup=lambda _account, _point: _priceable_tariff(),
    )
    earlier = replace(
        _record(),
        reading=replace(
            _record().reading,
            start_at=datetime(2026, 8, 2, 0, tzinfo=UTC),
            end_at=datetime(2026, 8, 2, 0, 30, tzinfo=UTC),
        ),
    )

    ledger = _Ledger((earlier, _record()))
    # The first pass always republishes: nothing is known yet about what the cost was last
    # computed from. The correction this test is about happens on a later one.
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    published.clear()

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=datetime(2026, 8, 3, tzinfo=UTC),
    )

    rows = next(args[2] for args in published if args[1]["statistic_id"].endswith("_tariff_cost"))
    # Only the dirty hour is republished, and its sum still includes the earlier day.
    assert len(rows) == 1
    assert rows[0]["sum"] > rows[0]["state"]


async def test_an_archive_with_nothing_in_it_prices_from_the_reported_rate(
    hass: HomeAssistant,
) -> None:
    """An unreadable archive is quarantined and answers empty, and says so in a warning:

        Past hours will be priced from the rate the provider reports now

    Taking the lookup as the only source made that unkeepable — an empty answer priced every
    hour with no fuel-cost adjustment and no levy, which is silently low rather than missing.
    The reported rate is still in hand, so it is used.
    """
    from custom_components.octopus_energy_japan.tariff_history import (
        AdderSchedule,
        live_schedule,
    )

    hass.config.components.add(RECORDER)

    async def _first_cost(adder_lookup: object) -> Decimal:
        published: list[tuple[object, ...]] = []
        projector = HomeAssistantStatisticsProjector(
            hass,
            SECRET,
            publisher=lambda *args: published.append(args),
            cleaner=Mock(),
            tariff_lookup=lambda _account, _point: _priceable_tariff(),
            adder_lookup=adder_lookup,  # type: ignore[arg-type]
        )
        await projector.async_project_supply_point(
            _Ledger((_record(),)),  # type: ignore[arg-type]
            "A-1",
            "SP-1",
            NOW,
            dirty_from=None,
        )
        rows = next(
            args[2] for args in published if args[1]["statistic_id"].endswith("_tariff_cost")
        )
        return Decimal(str(rows[0]["state"]))

    # What the archive holds in the ordinary case: the periods the reported tariff carries.
    archived = live_schedule(_priceable_tariff(), observed_at=NOW)  # type: ignore[arg-type]
    filled = await _first_cost(lambda _account, _point: archived)
    quarantined = await _first_cost(lambda _account, _point: AdderSchedule())

    assert quarantined == filled
    # And the adders are not a rounding difference: without them the hour costs less.
    bare = replace(
        _priceable_tariff(),  # type: ignore[type-var]
        fuel_cost_adjustment=None,
        renewable_energy_levy=None,
    )
    published: list[tuple[object, ...]] = []
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
        tariff_lookup=lambda _account, _point: bare,
        adder_lookup=lambda _account, _point: AdderSchedule(),
    )
    await projector.async_project_supply_point(
        _Ledger((_record(),)),  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    rows = next(args[2] for args in published if args[1]["statistic_id"].endswith("_tariff_cost"))
    assert Decimal(str(rows[0]["state"])) < quarantined


class _RangedLedger(_Ledger):
    """A ledger that honours the requested window, so truncation is observable."""

    def __init__(
        self,
        records: tuple[LedgerRecord, ...],
        partitions: frozenset[str] = frozenset({"2026-07", "2026-08"}),
    ) -> None:
        super().__init__(records)
        self.known_partitions = partitions

    async def async_records(
        self,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[LedgerRecord, ...]:
        self.requested = (start_at, end_at)
        return tuple(
            record for record in self.records if start_at <= record.reading.start_at < end_at
        )


def _record_at(moment: datetime, value: str) -> LedgerRecord:
    return replace(
        _record(value=value),
        reading=replace(
            _record(value=value).reading,
            start_at=moment,
            end_at=moment + timedelta(minutes=30),
        ),
    )


# 2026-07-31T15:00Z is 2026-08-01 00:00 JST, the boundary a projection may be truncated at
# for anything dirtied during August.
AUGUST_JST = datetime(2026, 7, 31, 15, tzinfo=UTC)
JULY_HOUR = datetime(2026, 7, 15, tzinfo=UTC)
AUGUST_HOUR = datetime(2026, 8, 3, tzinfo=UTC)


def _sum_at(published: list[tuple[object, ...]], suffix: str, start: datetime) -> float:
    for args in published:
        if not str(args[1]["statistic_id"]).endswith(suffix):  # type: ignore[index]
            continue
        for row in args[2]:  # type: ignore[index]
            if row["start"] == start:
                return float(row["sum"])
    raise AssertionError(f"No {suffix} row published for {start}")


async def test_a_truncated_projection_does_not_restart_the_cumulative_sum(
    hass: HomeAssistant,
) -> None:
    """The defect this guards against made a corrected hour look like the first ever recorded.

    Reading the whole ledger for every correction costs a pass over every month collected, so
    a later pass starts at the period boundary instead. Its sums have to resume from what the
    previous pass reached there, or the Energy dashboard reads the total as going backwards.
    """
    hass.config.components.add(RECORDER)
    published: list[tuple[object, ...]] = []
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
    )
    ledger = _RangedLedger((_record_at(JULY_HOUR, "1.0"), _record_at(AUGUST_HOUR, "0.5")))

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    assert ledger.requested == (datetime(2026, 7, 1, tzinfo=UTC), NOW)
    assert _sum_at(published, "_import_energy", AUGUST_HOUR) == pytest.approx(1.5)

    published.clear()
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=AUGUST_HOUR,
    )

    # Only August was read, and July's kilowatt-hour is still in the total.
    assert ledger.requested == (AUGUST_JST, NOW)
    assert _sum_at(published, "_import_energy", AUGUST_HOUR) == pytest.approx(1.5)


async def test_a_correction_older_than_the_remembered_boundaries_reads_everything(
    hass: HomeAssistant,
) -> None:
    """Two boundaries are remembered, which is what the refresh cadence reaches.

    An older correction is rare and must still be right, so it falls back to the whole
    ledger rather than resuming from a total that was never recorded.
    """
    hass.config.components.add(RECORDER)
    projector = HomeAssistantStatisticsProjector(hass, SECRET, publisher=Mock(), cleaner=Mock())
    partitions = frozenset({"2026-05", "2026-06", "2026-07", "2026-08"})
    ledger = _RangedLedger((_record_at(AUGUST_HOUR, "0.5"),), partitions)

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=datetime(2026, 6, 10, tzinfo=UTC),
    )

    assert ledger.requested == (datetime(2026, 5, 1, tzinfo=UTC), NOW)


async def test_a_correction_in_the_earliest_month_reads_everything(
    hass: HomeAssistant,
) -> None:
    """There is nothing before the first month to resume from, so truncating buys nothing."""
    hass.config.components.add(RECORDER)
    projector = HomeAssistantStatisticsProjector(hass, SECRET, publisher=Mock(), cleaner=Mock())
    ledger = _RangedLedger((_record_at(JULY_HOUR, "1.0"),))

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=JULY_HOUR,
    )

    # July's boundary is 2026-06-30T15:00Z, before the ledger begins.
    assert ledger.requested == (datetime(2026, 7, 1, tzinfo=UTC), NOW)


async def test_a_total_is_remembered_when_every_hour_precedes_the_boundary(
    hass: HomeAssistant,
) -> None:
    """The first reading of a new period has to continue from the previous one's total."""
    hass.config.components.add(RECORDER)
    published: list[tuple[object, ...]] = []
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
    )
    ledger = _RangedLedger((_record_at(JULY_HOUR, "1.0"),))

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    # August arrives after the boundary was remembered, which is the ordinary case.
    ledger.records = (*ledger.records, _record_at(AUGUST_HOUR, "0.5"))
    published.clear()
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=AUGUST_HOUR,
    )

    assert ledger.requested == (AUGUST_JST, NOW)
    assert _sum_at(published, "_import_energy", AUGUST_HOUR) == pytest.approx(1.5)


async def test_a_boundary_the_cadence_no_longer_reaches_is_forgotten(
    hass: HomeAssistant,
) -> None:
    """Two boundaries are kept, so the memory is bounded and cannot go stale.

    A total recorded at an old boundary stops being trustworthy once a correction can rewrite
    the hours before it, and keeping one per month collected would grow without limit.
    """
    hass.config.components.add(RECORDER)
    projector = HomeAssistantStatisticsProjector(hass, SECRET, publisher=Mock(), cleaner=Mock())
    october = datetime(2026, 10, 3, 3, tzinfo=UTC)
    ledger = _RangedLedger(
        (_record_at(JULY_HOUR, "1.0"), _record_at(AUGUST_HOUR, "0.5")),
        frozenset({"2026-07", "2026-08", "2026-09", "2026-10"}),
    )

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    assert AUGUST_JST in projector._baselines[("A-1", "SP-1")]

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        october,
        dirty_from=None,
    )

    assert set(projector._baselines[("A-1", "SP-1")]) == {
        datetime(2026, 8, 31, 15, tzinfo=UTC),
        datetime(2026, 9, 30, 15, tzinfo=UTC),
    }


async def test_changing_the_calendar_discards_the_remembered_totals(
    hass: HomeAssistant,
) -> None:
    """A total recorded at a boundary of one calendar says nothing about another's.

    Discovery can report a supply start after the first pass has already run against the
    calendar-month fallback, and resuming from the old total would place the step counter and
    the sums at instants the new calendar does not have.
    """
    hass.config.components.add(RECORDER)
    projector = HomeAssistantStatisticsProjector(hass, SECRET, publisher=Mock(), cleaner=Mock())
    ledger = _RangedLedger((_record_at(JULY_HOUR, "1.0"), _record_at(AUGUST_HOUR, "0.5")))
    anchored = BillingPeriodCalendar.from_supply_start(
        datetime(2026, 6, 17, 15, tzinfo=UTC),
        local_timezone=TOKYO,
    )

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    assert AUGUST_JST in projector._baselines[("A-1", "SP-1")]

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=AUGUST_HOUR,
        billing_periods=anchored,
    )

    # The whole ledger, because the fallback's boundary is meaningless to the new calendar.
    assert ledger.requested == (datetime(2026, 7, 1, tzinfo=UTC), NOW)
    assert AUGUST_JST not in projector._baselines[("A-1", "SP-1")]


async def test_a_deletion_driven_rebuild_still_reads_everything(hass: HomeAssistant) -> None:
    """A reset clears the series, so its replacement cannot resume from a stored total."""
    hass.config.components.add(RECORDER)
    projector = HomeAssistantStatisticsProjector(hass, SECRET, publisher=Mock(), cleaner=Mock())
    ledger = _RangedLedger((_record_at(JULY_HOUR, "1.0"), _record_at(AUGUST_HOUR, "0.5")))

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    with patch("custom_components.octopus_energy_japan.statistics_runtime.get_instance"):
        await projector.async_project_supply_point(
            ledger,  # type: ignore[arg-type]
            "A-1",
            "SP-1",
            NOW,
            dirty_from=AUGUST_HOUR,
            reset_directions=frozenset({ReadingDirection.IMPORT}),
        )

    assert ledger.requested == (datetime(2026, 7, 1, tzinfo=UTC), NOW)


async def test_a_quiet_tariff_lookup_does_not_lose_the_cost_total(
    hass: HomeAssistant,
) -> None:
    """A refresh that cannot read the tariff publishes no cost rows, and must not forget.

    Dropping the cost total would make the next truncated pass publish sums starting again
    from zero, which the Energy dashboard reads as the cost history collapsing.
    """
    hass.config.components.add(RECORDER)
    published: list[tuple[object, ...]] = []
    tariff: list[object | None] = [_priceable_tariff()]
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
        tariff_lookup=lambda _account, _point: tariff[0],  # type: ignore[arg-type,return-value]
    )
    ledger = _RangedLedger((_record_at(JULY_HOUR, "1.0"), _record_at(AUGUST_HOUR, "0.5")))

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    whole_history = _sum_at(published, "_tariff_cost", AUGUST_HOUR)

    tariff[0] = None
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=AUGUST_HOUR,
    )

    tariff[0] = _priceable_tariff()
    published.clear()
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=AUGUST_HOUR,
    )

    # The tariff coming back is itself a change to what the cost is computed from, so this pass
    # reprices and reads the whole ledger. What matters is that the total is the one it was
    # before the lookup went quiet, not a series starting again from zero.
    assert _sum_at(published, "_tariff_cost", AUGUST_HOUR) == pytest.approx(whole_history)


async def test_export_energy_never_gets_a_cost_series(hass: HomeAssistant) -> None:
    hass.config.components.add(RECORDER)
    published: list[tuple[object, ...]] = []
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
        tariff_lookup=lambda _account, _point: _priceable_tariff(),
    )

    await projector.async_project_supply_point(
        _Ledger((_record(direction=ReadingDirection.EXPORT),)),  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    assert published
    assert not any(args[1]["statistic_id"].endswith("_tariff_cost") for args in published)


async def test_a_changed_price_republishes_the_whole_cost_history(
    hass: HomeAssistant,
) -> None:
    """Without this, a corrected price is computed for every hour and then discarded.

    `dirty_from` limits publication to recent hours, so a change to a price, a period boundary,
    or an archived adjustment would never reach the rows an earlier version of this formula
    wrote. Energy rows are untouched, because a price does not move them.
    """
    hass.config.components.add(RECORDER)
    published: list[tuple[object, ...]] = []
    tariff: list[object] = [_priceable_tariff()]
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
        tariff_lookup=lambda _account, _point: tariff[0],  # type: ignore[arg-type,return-value]
    )
    ledger = _RangedLedger((_record_at(JULY_HOUR, "1.0"), _record_at(AUGUST_HOUR, "0.5")))

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    # A dirty boundary that would ordinarily publish only August.
    published.clear()
    tariff[0] = _priceable_tariff(step_price="30.00")
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=AUGUST_HOUR,
    )

    cost_starts = [
        row["start"]
        for args in published
        if str(args[1]["statistic_id"]).endswith("_tariff_cost")  # type: ignore[index]
        for row in args[2]  # type: ignore[index]
    ]
    energy_starts = [
        row["start"]
        for args in published
        if str(args[1]["statistic_id"]).endswith("_import_energy")  # type: ignore[index]
        for row in args[2]  # type: ignore[index]
    ]
    assert JULY_HOUR in cost_starts
    assert JULY_HOUR not in energy_starts


async def test_an_unchanged_price_leaves_the_past_alone(hass: HomeAssistant) -> None:
    """Republishing on every refresh would rewrite the whole cost history twice an hour."""
    hass.config.components.add(RECORDER)
    published: list[tuple[object, ...]] = []
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
        tariff_lookup=lambda _account, _point: _priceable_tariff(),
    )
    ledger = _RangedLedger((_record_at(JULY_HOUR, "1.0"), _record_at(AUGUST_HOUR, "0.5")))

    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    published.clear()
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=AUGUST_HOUR,
    )

    cost_starts = [
        row["start"]
        for args in published
        if str(args[1]["statistic_id"]).endswith("_tariff_cost")  # type: ignore[index]
        for row in args[2]  # type: ignore[index]
    ]
    assert cost_starts == [AUGUST_HOUR]


async def test_a_renamed_device_renames_its_statistics(hass: HomeAssistant) -> None:
    """The Energy dashboard picker shows this name and nothing else.

    Reading only the generated name meant an installation with two logins saw two
    identically named series there, even after renaming both devices to tell them apart.
    """
    from custom_components.octopus_energy_japan.const import DOMAIN as OEJP_DOMAIN
    from custom_components.octopus_energy_japan.identity import (
        stable_supply_point_identity,
    )
    from homeassistant.helpers import device_registry as dr

    hass.config.components.add(RECORDER)
    identity = stable_supply_point_identity(SECRET, "A-1", "SP-1")
    entry = MockConfigEntry(domain=OEJP_DOMAIN)
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(OEJP_DOMAIN, identity)},
        name="OEJP supply point 1-1",
    )
    registry.async_update_device(device.id, name_by_user="Old flat")

    published: list[tuple[object, ...]] = []
    projector = HomeAssistantStatisticsProjector(
        hass,
        SECRET,
        publisher=lambda *args: published.append(args),
        cleaner=Mock(),
    )
    await projector.async_project_supply_point(
        _Ledger((_record(),)),  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )

    names = {args[1]["name"] for args in published}
    assert names == {"Old flat Import energy"}


@pytest.mark.recorder_harness
async def test_an_hour_withdrawn_from_the_middle_leaves_no_stale_row(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """A reading the provider withdraws must take its published row with it.

    Rows are written by statistic id and hour, so an hour that stops being projected is not
    overwritten by anything — it is simply left behind, carrying a cumulative from before the
    withdrawal. The next hour then resumes lower, and the Energy Dashboard draws the drop as a
    negative day. That is what a real installation showed on 2026-08-13.
    """
    first = datetime(2026, 8, 3, 0, tzinfo=UTC)
    middle = datetime(2026, 8, 3, 1, tzinfo=UTC)
    last = datetime(2026, 8, 3, 2, tzinfo=UTC)
    ledger = _Ledger(
        (
            _record_at(first, "1.0"),
            _record_at(middle, "2.0"),
            _record_at(last, "4.0"),
        )
    )
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
        partial(get_metadata, hass, statistic_source="octopus_energy_japan")
    )
    statistic_id = next(iter(metadata))

    async def _sums() -> list[float]:
        rows = await hass.async_add_executor_job(
            statistics_during_period,
            hass,
            first,
            NOW,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
        return [row["sum"] for row in rows[statistic_id]]

    assert await _sums() == [1.0, 3.0, 7.0]

    # The provider withdraws the middle hour: it is gone from the ledger, and the projection
    # no longer contains it.
    ledger.records = (_record_at(first, "1.0"), _record_at(last, "4.0"))
    await projector.async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=middle,
        reset_directions=frozenset({ReadingDirection.IMPORT}),
    )
    await async_recorder_block_till_done(hass)

    # Two rows, and the cumulative never goes backwards. A third row here would be the
    # withdrawn hour left behind at 3.0, ahead of a 5.0 that follows it.
    assert await _sums() == [1.0, 5.0]


@pytest.mark.recorder_harness
async def test_a_withdrawal_lost_to_a_restart_leaves_the_row_behind(
    recorder_mock: Recorder,
    hass: HomeAssistant,
) -> None:
    """A restart drops the instruction to clear, so the pass must decide for itself.

    `reset_directions` is set when the ledger deletes a reading and is carried in the
    coordinator's in-memory pending map. If Home Assistant restarts between the deletion and
    the projection — which is exactly what an integration upgrade does — the next pass
    projects the whole series again but never clears it. Rows for hours that no longer exist
    stay behind, holding a cumulative from before the withdrawal.

    This is the shape a real installation was found in on 2026-08-13: rows continuing past the
    withdrawal, then the next real hour resuming lower, drawn as a negative day.
    """
    first = datetime(2026, 8, 3, 0, tzinfo=UTC)
    middle = datetime(2026, 8, 3, 1, tzinfo=UTC)
    last = datetime(2026, 8, 3, 2, tzinfo=UTC)
    ledger = _Ledger(
        (
            _record_at(first, "1.0"),
            _record_at(middle, "2.0"),
            _record_at(last, "4.0"),
        )
    )
    await HomeAssistantStatisticsProjector(hass, SECRET).async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    await async_recorder_block_till_done(hass)

    metadata = await hass.async_add_executor_job(
        partial(get_metadata, hass, statistic_source="octopus_energy_japan")
    )
    statistic_id = next(iter(metadata))

    async def _sums() -> list[float]:
        rows = await hass.async_add_executor_job(
            statistics_during_period,
            hass,
            first,
            NOW,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
        return [row["sum"] for row in rows[statistic_id]]

    assert await _sums() == [1.0, 3.0, 7.0]

    # The middle hour is withdrawn, and a restart loses the reset before it is acted on. A
    # fresh projector is what a restart produces: no remembered fingerprints, no pending
    # reset, so the pass republishes from the beginning without clearing.
    ledger.records = (_record_at(first, "1.0"), _record_at(last, "4.0"))
    await HomeAssistantStatisticsProjector(hass, SECRET).async_project_supply_point(
        ledger,  # type: ignore[arg-type]
        "A-1",
        "SP-1",
        NOW,
        dirty_from=None,
    )
    await async_recorder_block_till_done(hass)

    assert await _sums() == [1.0, 5.0]
