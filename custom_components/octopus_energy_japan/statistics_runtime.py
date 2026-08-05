"""Home Assistant recorder adapter for deterministic OEJP statistics."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
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
from .billing_period import BillingPeriodCalendar
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

# The provider's timezone, which is a property of the provider rather than an assumption about
# a region: Japan has one timezone and no daylight saving, and the rate validity windows
# observed on a real account were JST calendar months — the fuel adjustment ran 2026-08-01 to
# 2026-09-01 JST and the levy from 2026-05-01 JST. Tariff steps restart on the account's
# reported meter-reading day within this timezone, not on its calendar month; the calendar month
# is only the fallback when no reading day is reported.
TOKYO = ZoneInfo("Asia/Tokyo")

# The fallback calendar, used when the supply start date is unknown. It is also what decides
# where a projection may be truncated: steps restart on a period boundary, so a pass starting
# on one computes every later hour exactly as a whole-ledger pass would.
CALENDAR_MONTHS = BillingPeriodCalendar.calendar_months(TOKYO)


def _hour_start(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _cumulative_before(
    series: tuple[tuple[datetime, Decimal], ...],
    boundary: datetime,
    fallback: Decimal,
) -> Decimal:
    """Return the cumulative total at the last hour before ``boundary``."""
    total = fallback
    for start, cumulative in series:
        if start >= boundary:
            break
        total = cumulative
    return total


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
        billing_periods: BillingPeriodCalendar = CALENDAR_MONTHS,
    ) -> None:
        """Project one supply point, pricing its steps over `billing_periods`."""


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
        # Per supply point, the cumulative total each statistic had reached at a period
        # boundary. Held in memory only: the projector is the sole writer of these series,
        # so its own last pass is the truth, and an empty cache costs one whole-ledger pass.
        self._baselines: dict[tuple[str, str], dict[datetime, dict[str, Decimal]]] = {}
        # Which calendar those totals were computed under, so a change discards them.
        self._calendars: dict[tuple[str, str], BillingPeriodCalendar] = {}

    async def async_project_supply_point(
        self,
        ledger: PersistentIntervalLedger,
        account_id: str,
        supply_point_id: str,
        generated_at: datetime,
        *,
        dirty_from: datetime | None,
        reset_directions: frozenset[ReadingDirection] = frozenset(),
        billing_periods: BillingPeriodCalendar = CALENDAR_MONTHS,
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
        earliest, _end_at = partition_bounds(min(ledger.known_partitions))
        scope = (account_id, supply_point_id)
        effective_dirty = None if reset_directions else dirty_from
        start_at, baseline = self._projection_start(
            scope,
            earliest,
            effective_dirty,
            billing_periods,
        )
        records = tuple(
            record
            for record in await ledger.async_records(start_at, generated_at)
            if record.reading.account_id == account_id
            and record.reading.supply_point_id == supply_point_id
        )
        # Projected without a dirty boundary, then filtered at publication. Every cumulative
        # sum is computed from every record in the pass, and a pass that starts mid-history
        # resumes from `baseline`: handing a series a pre-filtered set with *no* baseline
        # restarted its running total at the correction, which made a corrected hour look
        # like the first hour ever recorded.
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
        totals: dict[str, tuple[tuple[datetime, Decimal], ...]] = {}
        for series in projection.series:
            if series.key.kind is StatisticKind.OFFICIAL_COST and not self._include_official_cost:
                continue
            statistic_id = statistic_id_for(identity, series.key.direction, series.key.kind)
            offset = baseline.get(statistic_id, Decimal(0))
            cumulative: list[tuple[datetime, Decimal]] = []
            statistics: list[StatisticData] = []
            for value in series.statistics:
                total = offset + value.sum
                cumulative.append((value.start, total))
                if boundary is None or value.start >= boundary:
                    statistics.append(
                        StatisticData(
                            start=value.start,
                            state=float(value.state),
                            sum=float(total),
                        )
                    )
            totals[statistic_id] = tuple(cumulative)
            if statistics:
                self._publisher(
                    self._hass,
                    _metadata(self._hass, identity, series),
                    tuple(statistics),
                )

        totals.update(
            self._publish_tariff_cost(
                account_id,
                supply_point_id,
                identity,
                projection.series,
                dirty_from=effective_dirty,
                baseline=baseline,
                periods=billing_periods,
            )
        )
        self._remember_baselines(
            scope,
            start_at,
            generated_at,
            totals,
            baseline,
            billing_periods,
        )

    def _projection_start(
        self,
        scope: tuple[str, str],
        earliest: datetime,
        dirty_from: datetime | None,
        periods: BillingPeriodCalendar,
    ) -> tuple[datetime, Mapping[str, Decimal]]:
        """Choose where to start projecting, and the totals to resume the sums from.

        Reading the whole ledger for every correction costs one pass over every month ever
        collected, which grows without bound. Truncating is only safe on a period boundary:
        the cost series restarts its step counter there, so a pass that begins on one prices
        every later hour exactly as a whole-ledger pass would.

        Falls back to the whole ledger whenever that boundary has no remembered totals. That
        is the first pass after a restart, and it repairs itself by remembering them. Changing
        which calendar applies discards them too: a total recorded at a boundary of the old
        calendar says nothing about a boundary of the new one.
        """
        if self._calendars.get(scope) != periods:
            self._calendars[scope] = periods
            self._baselines.pop(scope, None)
            return earliest, {}
        if dirty_from is None:
            return earliest, {}
        candidate = periods.period_start(dirty_from)
        if candidate <= earliest:
            return earliest, {}
        remembered = self._baselines.get(scope, {}).get(candidate)
        if remembered is None:
            return earliest, {}
        return candidate, remembered

    def _remember_baselines(
        self,
        scope: tuple[str, str],
        start_at: datetime,
        generated_at: datetime,
        totals: Mapping[str, tuple[tuple[datetime, Decimal], ...]],
        baseline: Mapping[str, Decimal],
        periods: BillingPeriodCalendar,
    ) -> None:
        """Keep the cumulative totals at the two most recent period boundaries.

        Two is what the refresh cadence reaches: the poll re-reads the last 72 hours, and the
        daily reconciliation covers the current and previous month. A correction older than
        that falls back to a whole-ledger pass, which is correct and rare.

        A statistic missing from this pass keeps the total it was last remembered with. The
        tariff lookup can go quiet for a refresh, and dropping the cost baseline then would
        make the next truncated pass publish sums that start again from zero.
        """
        boundaries = (
            periods.previous_period_start(generated_at),
            periods.period_start(generated_at),
        )
        remembered = self._baselines.setdefault(scope, {})
        for stale in tuple(remembered):
            if stale not in boundaries:
                del remembered[stale]
        for at in boundaries:
            if at < start_at:
                continue
            remembered[at] = {
                **remembered.get(at, {}),
                **{
                    statistic_id: _cumulative_before(
                        cumulative,
                        at,
                        baseline.get(statistic_id, Decimal(0)),
                    )
                    for statistic_id, cumulative in totals.items()
                },
            }

    def _publish_tariff_cost(
        self,
        account_id: str,
        supply_point_id: str,
        identity: str,
        series: tuple[StatisticsSeriesProjection, ...],
        *,
        dirty_from: datetime | None,
        baseline: Mapping[str, Decimal],
        periods: BillingPeriodCalendar,
    ) -> tuple[tuple[str, tuple[tuple[datetime, Decimal], ...]], ...]:
        """Publish a cost series derived from the reported tariff, when one is known.

        The Energy dashboard cannot price an external statistic itself — it builds a cost
        sensor only for a real entity — so a cost statistic published here is the only way
        `stat_cost` can be filled. See `docs/ARCHITECTURE.md`.

        Returns the cumulative total each cost series reached, so the caller can remember it
        at the next period boundary.
        """
        if self._tariff_lookup is None:
            return ()
        tariff = self._tariff_lookup(account_id, supply_point_id)
        if tariff is None or not tariff.is_priceable:
            return ()

        totals: list[tuple[str, tuple[tuple[datetime, Decimal], ...]]] = []
        for energy in series:
            if energy.key.kind is not StatisticKind.ENERGY:
                continue
            if energy.key.direction is not ReadingDirection.IMPORT:
                # Export is compensated separately and is never priced as consumption.
                continue
            costs = project_hourly_cost(
                [(value.start, value.state) for value in energy.statistics],
                tariff,
                periods=periods,
                direction=energy.key.direction,
            )
            if not costs:
                continue
            cost_series = StatisticsSeriesProjection(
                key=replace(energy.key, kind=StatisticKind.TARIFF_COST),
                statistics=(),
            )
            statistic_id = statistic_id_for(
                identity,
                cost_series.key.direction,
                cost_series.key.kind,
            )
            running = baseline.get(statistic_id, Decimal(0))
            cumulative: list[tuple[datetime, Decimal]] = []
            rows: list[StatisticData] = []
            for cost in costs:
                running += cost.amount
                cumulative.append((cost.start, running))
                if dirty_from is not None and cost.start < _hour_start(dirty_from):
                    continue
                rows.append(
                    StatisticData(
                        start=cost.start,
                        state=float(cost.amount),
                        sum=float(running),
                    )
                )
            totals.append((statistic_id, tuple(cumulative)))
            if not rows:
                continue
            self._publisher(
                self._hass,
                _metadata(self._hass, identity, cost_series),
                tuple(rows),
            )
        return tuple(totals)

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
            statistic_id_for(identity, direction, kind)
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
            statistic_id=statistic_id_for(
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
        statistic_id=statistic_id_for(
            supply_point_identity,
            series.key.direction,
            series.key.kind,
        ),
        unit_class=None,
        unit_of_measurement=CURRENCY_JPY,
    )


def statistic_id_for(
    supply_point_identity: str,
    direction: ReadingDirection,
    kind: StatisticKind,
) -> str:
    """Name the external statistic one supply point's direction and kind publishes to.

    Public because entry removal has to delete these rows, and by then the entry is
    unloaded: the id is rebuilt from the identity encoded in the store filenames rather
    than from the runtime data, which is gone.
    """
    digest = supply_point_identity.rsplit("-", maxsplit=1)[-1]
    return f"{DOMAIN}:sp_{digest}_{direction.value}_{kind.value}"
