"""Home Assistant recorder adapter for deterministic OEJP statistics."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util.unit_conversion import EnergyConverter

from .api import ReadingDirection
from .const import DOMAIN
from .identity import stable_supply_point_identity
from .ledger import PersistentIntervalLedger, partition_bounds
from .statistics import (
    StatisticKind,
    StatisticsSeriesProjection,
    project_hourly_statistics,
)

type StatisticsPublisher = Callable[
    [HomeAssistant, StatisticMetaData, tuple[StatisticData, ...]],
    None,
]


class StatisticsProjector(Protocol):
    """Persistence boundary used by the runtime coordinator."""

    async def async_project_supply_point(
        self,
        ledger: PersistentIntervalLedger,
        account_id: str,
        supply_point_id: str,
        generated_at: datetime,
        *,
        dirty_from: datetime | None,
        reset_directions: frozenset[ReadingDirection] = frozenset(),
    ) -> None:
        """Project one supply point from its complete available ledger."""


class HomeAssistantStatisticsProjector:
    """Publish correction-safe hourly rows through the recorder public API."""

    def __init__(
        self,
        hass: HomeAssistant,
        identity_secret: str,
        *,
        include_official_cost: bool = False,
        publisher: StatisticsPublisher = async_add_external_statistics,
    ) -> None:
        self._hass = hass
        self._identity_secret = identity_secret
        self._include_official_cost = include_official_cost
        self._publisher = publisher

    async def async_project_supply_point(
        self,
        ledger: PersistentIntervalLedger,
        account_id: str,
        supply_point_id: str,
        generated_at: datetime,
        *,
        dirty_from: datetime | None,
        reset_directions: frozenset[ReadingDirection] = frozenset(),
    ) -> None:
        """Recalculate sums and replace the affected recorder projection."""
        if not ledger.known_partitions:
            if reset_directions:
                self._clear_directions(
                    account_id,
                    supply_point_id,
                    reset_directions,
                )
            return
        start_at, _end_at = partition_bounds(min(ledger.known_partitions))
        records = tuple(
            record
            for record in await ledger.async_records(start_at, generated_at)
            if record.reading.account_id == account_id
            and record.reading.supply_point_id == supply_point_id
        )
        projection = project_hourly_statistics(
            records,
            generated_at,
            dirty_from=None if reset_directions else dirty_from,
        )
        identity = stable_supply_point_identity(
            self._identity_secret,
            account_id,
            supply_point_id,
        )
        if reset_directions:
            self._clear_directions(
                account_id,
                supply_point_id,
                reset_directions,
            )
        for series in projection.series:
            if series.key.kind is StatisticKind.OFFICIAL_COST and not self._include_official_cost:
                continue
            statistics = tuple(
                StatisticData(
                    start=value.start,
                    state=float(value.state),
                    sum=float(value.sum),
                )
                for value in series.statistics
            )
            if statistics:
                self._publisher(
                    self._hass,
                    _metadata(identity, series),
                    statistics,
                )

    def _clear_directions(
        self,
        account_id: str,
        supply_point_id: str,
        directions: frozenset[ReadingDirection],
    ) -> None:
        """Remove stale rows before a deletion-driven full series rebuild."""
        identity = stable_supply_point_identity(
            self._identity_secret,
            account_id,
            supply_point_id,
        )
        kinds = [StatisticKind.ENERGY]
        if self._include_official_cost:
            kinds.append(StatisticKind.OFFICIAL_COST)
        statistic_ids = [
            _statistic_id(identity, direction, kind)
            for direction in sorted(directions, key=lambda value: value.value)
            for kind in kinds
        ]
        if not statistic_ids:
            return

        # Clear and subsequent imports use the same Recorder FIFO queue. Waiting
        # for a callback would unnecessarily block if Recorder is stopping.
        get_instance(self._hass).async_clear_statistics(statistic_ids)


def _metadata(
    supply_point_identity: str,
    series: StatisticsSeriesProjection,
) -> StatisticMetaData:
    digest = supply_point_identity.rsplit("-", maxsplit=1)[-1]
    direction = series.key.direction.value.title()
    safe_suffix = digest[:8]
    if series.key.kind is StatisticKind.ENERGY:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"OEJP {direction} energy {safe_suffix}",
            source=DOMAIN,
            statistic_id=_statistic_id(
                supply_point_identity,
                series.key.direction,
                series.key.kind,
            ),
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"OEJP {direction} official cost {safe_suffix}",
        source=DOMAIN,
        statistic_id=_statistic_id(
            supply_point_identity,
            series.key.direction,
            series.key.kind,
        ),
        unit_class=None,
        unit_of_measurement="JPY",
    )


def _statistic_id(
    supply_point_identity: str,
    direction: ReadingDirection,
    kind: StatisticKind,
) -> str:
    digest = supply_point_identity.rsplit("-", maxsplit=1)[-1]
    return f"{DOMAIN}:sp_{digest}_{direction.value}_{kind.value}"
