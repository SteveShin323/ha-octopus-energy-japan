"""Home Assistant recorder adapter for deterministic OEJP statistics."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.recorder import get_instance
from homeassistant.util.unit_conversion import EnergyConverter

from .api import ReadingDirection
from .api.tariff import SupplyPointTariff
from .const import CURRENCY_JPY, DOMAIN
from .identity import stable_supply_point_identity
from .ledger import PersistentIntervalLedger, partition_bounds
from .statistics import (
    StatisticKind,
    StatisticsSeriesProjection,
    project_hourly_statistics,
)
from .tariff_cost import project_hourly_cost

type StatisticsPublisher = Callable[
    [HomeAssistant, StatisticMetaData, tuple[StatisticData, ...]],
    None,
]
type TariffLookup = Callable[[str, str], SupplyPointTariff | None]

_LOGGER = logging.getLogger(__name__)

# The manifest lists `recorder` under `after_dependencies`, which orders setup when the
# recorder is going to be loaded but does not require it. A configuration without
# `recorder:` therefore reaches this module with no instance, and asking for one raised
# `KeyError: recorder_instance` — observed against a real account on 2026-08-04 on an
# instance without the recorder. Consumption entities do not need it; only Energy
# Dashboard statistics do, so those are skipped rather than allowed to fail the refresh.
_RECORDER_DOMAIN = "recorder"

# Tariff steps accumulate over the Asia/Tokyo month. The provider's own rate validity
# windows are JST calendar months — the fuel adjustment observed on 2026-08-04 ran from
# 2026-08-01 00:00 JST to 2026-09-01 00:00 JST, and the levy from 2026-05-01 JST — so that
# is the boundary this follows. It is not the billing period, which ends at a meter read a
# few hours after midnight; `docs/ARCHITECTURE.md` records the measured difference.
TOKYO = ZoneInfo("Asia/Tokyo")


def _hour_start(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


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
        tariff_lookup: TariffLookup | None = None,
    ) -> None:
        self._hass = hass
        self._identity_secret = identity_secret
        self._include_official_cost = include_official_cost
        self._publisher = publisher
        self._tariff_lookup = tariff_lookup
        self._warned_about_recorder = False

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
        if not self._recorder_available():
            return
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
        # Projected without a dirty boundary, then filtered at publication. The cumulative
        # sums are computed from every record either way, but the cost series has to
        # accumulate over the *whole* history before its tail is published: handing it a
        # pre-filtered series restarted its running total at the correction, which made a
        # corrected hour look like the first hour ever recorded.
        effective_dirty = None if reset_directions else dirty_from
        projection = project_hourly_statistics(records, generated_at)
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
        boundary = _hour_start(effective_dirty) if effective_dirty is not None else None
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
                if boundary is None or value.start >= boundary
            )
            if statistics:
                self._publisher(
                    self._hass,
                    _metadata(self._hass, identity, series),
                    statistics,
                )

        self._publish_tariff_cost(
            account_id,
            supply_point_id,
            identity,
            projection.series,
            dirty_from=effective_dirty,
        )

    def _publish_tariff_cost(
        self,
        account_id: str,
        supply_point_id: str,
        identity: str,
        series: tuple[StatisticsSeriesProjection, ...],
        *,
        dirty_from: datetime | None,
    ) -> None:
        """Publish a cost series derived from the reported tariff, when one is known.

        The Energy dashboard cannot price an external statistic itself — it builds a cost
        sensor only for a real entity — so a cost statistic published here is the only way
        `stat_cost` can be filled. See `docs/ARCHITECTURE.md`.
        """
        if self._tariff_lookup is None:
            return
        tariff = self._tariff_lookup(account_id, supply_point_id)
        if tariff is None or not tariff.is_priceable:
            return

        for energy in series:
            if energy.key.kind is not StatisticKind.ENERGY:
                continue
            if energy.key.direction is not ReadingDirection.IMPORT:
                # Export is compensated separately and is never priced as consumption.
                continue
            costs = project_hourly_cost(
                [(value.start, value.state) for value in energy.statistics],
                tariff,
                local_timezone=TOKYO,
                direction=energy.key.direction,
            )
            if not costs:
                continue
            running = Decimal(0)
            rows: list[StatisticData] = []
            for cost in costs:
                running += cost.amount
                if dirty_from is not None and cost.start < _hour_start(dirty_from):
                    continue
                rows.append(
                    StatisticData(
                        start=cost.start,
                        state=float(cost.amount),
                        sum=float(running),
                    )
                )
            if not rows:
                continue
            cost_series = StatisticsSeriesProjection(
                key=replace(energy.key, kind=StatisticKind.TARIFF_COST),
                statistics=(),
            )
            self._publisher(
                self._hass,
                _metadata(self._hass, identity, cost_series),
                tuple(rows),
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

    def _recorder_available(self) -> bool:
        """Report whether statistics can be written at all.

        Warned once per projector, so a reload logs it again but a refresh does not.
        Repeating it every thirty minutes would fill the log for a configuration choice
        the user made deliberately.
        """
        if _RECORDER_DOMAIN in self._hass.config.components:
            return True
        if not self._warned_about_recorder:
            self._warned_about_recorder = True
            _LOGGER.warning(
                "The Home Assistant recorder is not enabled, so Octopus Energy Japan "
                "cannot publish Energy Dashboard statistics. Consumption sensors and "
                "calendar totals are unaffected. Add `recorder:` to configuration.yaml "
                "to enable them"
            )
        return False


def _statistic_name(
    hass: HomeAssistant,
    supply_point_identity: str,
    series: StatisticsSeriesProjection,
    what: str,
) -> str:
    """Name a statistic after the device it belongs to.

    The Energy dashboard picker shows this name and nothing else, so an identity digest
    there is unreadable: a household with two supply points cannot tell which is which.
    The supply-point device is already named with a per-account ordinal, so reusing it
    keeps one human label instead of two schemes that can drift apart. It still contains
    no account number, supply-point number, or address.
    """
    direction = series.key.direction.value.title()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, supply_point_identity)})
    label = device.name if device is not None and device.name else None
    if label is None:
        # Only before the device registry has caught up, which self-corrects on the
        # next refresh. Never a provider identifier.
        label = f"OEJP {supply_point_identity.rsplit('-', maxsplit=1)[-1][:8]}"
    return f"{label} {direction} {what}"


def _metadata(
    hass: HomeAssistant,
    supply_point_identity: str,
    series: StatisticsSeriesProjection,
) -> StatisticMetaData:
    if series.key.kind is StatisticKind.ENERGY:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=_statistic_name(hass, supply_point_identity, series, "energy"),
            source=DOMAIN,
            statistic_id=_statistic_id(
                supply_point_identity,
                series.key.direction,
                series.key.kind,
            ),
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
    # Both remaining kinds are money. "cost" is the tariff-derived one a user selects in
    # the Energy dashboard; "official cost" is the provider's own per-interval estimate,
    # which is not published — see `docs/API_CONTRACTS.md`.
    what = "cost" if series.key.kind is StatisticKind.TARIFF_COST else "official cost"
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=_statistic_name(hass, supply_point_identity, series, what),
        source=DOMAIN,
        statistic_id=_statistic_id(
            supply_point_identity,
            series.key.direction,
            series.key.kind,
        ),
        unit_class=None,
        unit_of_measurement=CURRENCY_JPY,
    )


def _statistic_id(
    supply_point_identity: str,
    direction: ReadingDirection,
    kind: StatisticKind,
) -> str:
    digest = supply_point_identity.rsplit("-", maxsplit=1)[-1]
    return f"{DOMAIN}:sp_{digest}_{direction.value}_{kind.value}"
