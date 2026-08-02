"""Runtime discovery, reading synchronization, and aggregation coordinator."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aggregation import (
    TOKYO,
    AggregationSnapshot,
    SupplyPointAggregation,
    aggregate_calendar,
)
from .api import (
    AuthenticatedGraphQLClient,
    CapabilitySnapshot,
    GenericReadingsProvider,
    LegacyHalfHourlyProvider,
    OejpAccount,
    OejpAuthenticationError,
    OejpError,
    OejpSupplyPoint,
    ReadingDirection,
    ReadingFallbackReason,
    ReadingProviderName,
    ReadingProviderRouter,
    ResourceLifecycle,
    candidate_directions,
)
from .const import DOMAIN
from .identity import stable_account_identity, stable_supply_point_identity
from .ledger import (
    CorrectionResult,
    LedgerError,
    LedgerRecord,
    PersistentIntervalLedger,
    expand_authoritative_series,
)
from .ledger_store import HomeAssistantLedgerBackend
from .runtime import selected_historical_resources
from .sync import (
    POLL_INTERVAL,
    SyncScheduleState,
    SyncWindow,
    SyncWindowPlanner,
    slow_cadence_due,
)

_LOGGER = logging.getLogger(__name__)

type DiscoveryLoader = Callable[
    [],
    Awaitable[tuple[tuple[OejpAccount, ...], CapabilitySnapshot]],
]
type SupplyPointKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """Safe provider-selection metadata for one supply point."""

    account_identity: str
    supply_point_identity: str
    direction: ReadingDirection
    provider: ReadingProviderName
    fallback_reason: ReadingFallbackReason | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class OejpCoordinatorData:
    """Immutable coordinator snapshot consumed by Home Assistant entities."""

    accounts: tuple[OejpAccount, ...]
    capabilities: CapabilitySnapshot
    aggregation: AggregationSnapshot
    present_supply_points: frozenset[SupplyPointKey]
    provider_observations: tuple[ProviderObservation, ...] = ()
    correction_count: int = 0
    last_refresh_change_count: int = 0
    corrupt_partition_count: int = 0

    def supply_point_aggregation(
        self,
        account_id: str,
        supply_point_id: str,
        direction: ReadingDirection,
    ) -> SupplyPointAggregation | None:
        """Return one direction projection without exposing it as entity metadata."""
        return next(
            (
                value
                for value in self.aggregation.supply_points
                if value.account_id == account_id
                and value.supply_point_id == supply_point_id
                and value.direction is direction
            ),
            None,
        )

    def supply_point_lifecycle(
        self,
        account_id: str,
        supply_point_id: str,
    ) -> ResourceLifecycle | None:
        """Return the latest discovered lifecycle for one supply point."""
        for account in self.accounts:
            if account.number != account_id:
                continue
            for point in iter_supply_points(account):
                if point.id == supply_point_id:
                    return point.lifecycle
        return None


@dataclass(slots=True)
class _SupplyPointRuntime:
    """Private raw-identifier state for one supply point."""

    supply_point: OejpSupplyPoint
    backend: HomeAssistantLedgerBackend
    ledger: PersistentIntervalLedger
    router: ReadingProviderRouter


class OejpDataUpdateCoordinator(DataUpdateCoordinator[OejpCoordinatorData]):
    """Synchronize enabled supply points into correction-aware local ledgers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AuthenticatedGraphQLClient,
        accounts: tuple[OejpAccount, ...],
        capabilities: CapabilitySnapshot,
        identity_secret: str,
        discovery_loader: DiscoveryLoader,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
            config_entry=entry,
        )
        self._entry = entry
        self._client = client
        self._accounts = accounts
        self._capabilities = capabilities
        self._identity_secret = identity_secret
        self._discovery_loader = discovery_loader
        self._now = now or (lambda: datetime.now(UTC))
        self._planner = SyncWindowPlanner()
        self._schedule = SyncScheduleState(last_discovery_at=self._utc_now())
        self._supply_points: dict[SupplyPointKey, _SupplyPointRuntime] = {}
        self._first_sync = True

    @property
    def accounts(self) -> tuple[OejpAccount, ...]:
        """Return the most recent discovered accounts."""
        return self._accounts

    @property
    def capabilities(self) -> CapabilitySnapshot:
        """Return the most recent capability snapshot."""
        return self._capabilities

    async def _async_update_data(self) -> OejpCoordinatorData:
        now = self._utc_now()
        try:
            await self._async_refresh_discovery_if_due(now)
            await self._async_prepare_enabled_supply_points(now)
            due = slow_cadence_due(now, self._schedule)
            if self._first_sync:
                windows = self._planner.initial(now)
            elif due.reconciliation:
                windows = self._planner.reconciliation(now)
            else:
                windows = self._planner.poll(now)

            observations: dict[
                tuple[str, str, ReadingDirection],
                ProviderObservation,
            ] = {}
            corrections: list[CorrectionResult] = []
            for state in self._enabled_states():
                for direction in candidate_directions(
                    state.supply_point,
                    self._capabilities,
                ):
                    for window in windows:
                        result, observation = await self._async_sync_window(
                            state,
                            direction,
                            window,
                        )
                        corrections.append(result)
                        observations[
                            (
                                state.supply_point.account_number,
                                state.supply_point.id,
                                direction,
                            )
                        ] = observation

            records: list[LedgerRecord] = []
            aggregate_start = _previous_local_month_start(now)
            for state in self._supply_points.values():
                records.extend(await state.ledger.async_records(aggregate_start, now))

            self._first_sync = False
            self._schedule = SyncScheduleState(
                last_reconciliation_date=now.astimezone(TOKYO).date(),
                last_discovery_at=self._schedule.last_discovery_at,
                last_contract_at=self._schedule.last_contract_at,
                last_billing_at=self._schedule.last_billing_at,
            )
            combined = CorrectionResult.combine(corrections)
            return OejpCoordinatorData(
                accounts=self._accounts,
                capabilities=self._capabilities,
                aggregation=aggregate_calendar(records, now),
                present_supply_points=frozenset(
                    (
                        point.account_number,
                        point.id,
                    )
                    for account in self._accounts
                    for point in iter_supply_points(account)
                ),
                provider_observations=tuple(observations[key] for key in sorted(observations)),
                correction_count=sum(record.correction_count for record in records),
                last_refresh_change_count=(
                    combined.inserted_count + combined.corrected_count + combined.deleted_count
                ),
                corrupt_partition_count=sum(
                    len(state.ledger.corrupt_partitions) for state in self._supply_points.values()
                ),
            )
        except OejpAuthenticationError as err:
            raise ConfigEntryAuthFailed("OEJP OAuth authorization must be renewed") from err
        except (OejpError, LedgerError, ValueError) as err:
            raise UpdateFailed("OEJP reading synchronization failed") from err

    async def async_shutdown_runtime(self) -> None:
        """Flush every debounced ledger write before config-entry unload."""
        for state in self._supply_points.values():
            await state.backend.async_flush()

    async def _async_refresh_discovery_if_due(self, now: datetime) -> None:
        due = slow_cadence_due(now, self._schedule)
        if not due.discovery:
            return
        accounts, capabilities = await self._discovery_loader()
        self._accounts = accounts
        self._capabilities = capabilities
        self._schedule = SyncScheduleState(
            last_reconciliation_date=self._schedule.last_reconciliation_date,
            last_discovery_at=now,
            last_contract_at=self._schedule.last_contract_at,
            last_billing_at=self._schedule.last_billing_at,
        )
        from .runtime import OejpRuntimeData, async_project_discovered_devices

        runtime = self._entry.runtime_data
        if isinstance(runtime, OejpRuntimeData):
            runtime.accounts = accounts
            runtime.capabilities = capabilities
            async_project_discovered_devices(self.hass, self._entry, runtime)

    async def _async_prepare_enabled_supply_points(self, now: datetime) -> None:
        for point in enabled_supply_points(
            self._entry,
            self._accounts,
            self._identity_secret,
        ):
            key = (point.account_number, point.id)
            state = self._supply_points.get(key)
            if state is None:
                storage_scope = stable_supply_point_identity(
                    self._identity_secret,
                    point.account_number,
                    point.id,
                )
                backend = HomeAssistantLedgerBackend(
                    self.hass,
                    self._entry.entry_id,
                    storage_scope,
                )
                ledger = PersistentIntervalLedger(
                    backend,
                    account_id=point.account_number,
                    supply_point_id=point.id,
                )
                await ledger.async_initialize(now)
                state = _SupplyPointRuntime(
                    supply_point=point,
                    backend=backend,
                    ledger=ledger,
                    router=self._reading_router(),
                )
                self._supply_points[key] = state
            else:
                state.supply_point = point
                state.router = self._reading_router()

    async def _async_sync_window(
        self,
        state: _SupplyPointRuntime,
        direction: ReadingDirection,
        window: SyncWindow,
    ) -> tuple[CorrectionResult, ProviderObservation]:
        direction_result = await state.router.async_get_readings(
            state.supply_point,
            direction,
            window.start_at,
            window.end_at,
        )
        existing = await state.ledger.async_records(
            window.start_at,
            window.end_at,
        )
        authoritative_series = expand_authoritative_series(
            direction_result.authoritative_series,
            direction_result.authoritative_sources,
            existing,
        )
        result = await state.ledger.async_reconcile(
            authoritative_series,
            window.start_at,
            window.end_at,
            direction_result.readings,
            direction_result.observed_at,
        )
        return result, ProviderObservation(
            account_identity=stable_account_identity(
                self._identity_secret,
                state.supply_point.account_number,
            ),
            supply_point_identity=stable_supply_point_identity(
                self._identity_secret,
                state.supply_point.account_number,
                state.supply_point.id,
            ),
            direction=direction,
            provider=direction_result.provider,
            fallback_reason=direction_result.fallback_reason,
            observed_at=direction_result.observed_at,
        )

    def _reading_router(self) -> ReadingProviderRouter:
        return ReadingProviderRouter(
            GenericReadingsProvider(
                self._client,
                self._capabilities,
                now=self._now,
            ),
            LegacyHalfHourlyProvider(
                self._client,
                self._capabilities,
                now=self._now,
            ),
            self._capabilities,
            now=self._now,
        )

    def _enabled_states(self) -> tuple[_SupplyPointRuntime, ...]:
        enabled = {
            (point.account_number, point.id)
            for point in enabled_supply_points(
                self._entry,
                self._accounts,
                self._identity_secret,
            )
        }
        return tuple(
            self._supply_points[key] for key in sorted(enabled) if key in self._supply_points
        )

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("Coordinator clock must be timezone-aware")
        return value.astimezone(UTC)


def iter_supply_points(account: OejpAccount) -> tuple[OejpSupplyPoint, ...]:
    """Return every supply point without selecting the first account/property."""
    return tuple(point for property_ in account.properties for point in property_.supply_points)


def enabled_supply_points(
    entry: ConfigEntry,
    accounts: Iterable[OejpAccount],
    identity_secret: str,
) -> tuple[OejpSupplyPoint, ...]:
    """Select active/unknown resources and explicitly enabled history."""
    selected = selected_historical_resources(entry)
    enabled: list[OejpSupplyPoint] = []
    for account in accounts:
        account_identity = stable_account_identity(identity_secret, account.number)
        account_enabled = (
            account.lifecycle is not ResourceLifecycle.HISTORICAL or account_identity in selected
        )
        if not account_enabled:
            continue
        for point in iter_supply_points(account):
            point_identity = stable_supply_point_identity(
                identity_secret,
                account.number,
                point.id,
            )
            if point.lifecycle is not ResourceLifecycle.HISTORICAL or point_identity in selected:
                enabled.append(point)
    return tuple(
        sorted(
            enabled,
            key=lambda point: (point.account_number, point.id),
        )
    )


def entity_directions(
    data: OejpCoordinatorData | None,
    identity_secret: str,
    account_id: str,
    supply_point_id: str,
) -> tuple[ReadingDirection, ...]:
    """Return only directions proven queryable by an authoritative success."""
    if data is None:
        return ()
    account_identity = stable_account_identity(identity_secret, account_id)
    point_identity = stable_supply_point_identity(
        identity_secret,
        account_id,
        supply_point_id,
    )
    return tuple(
        sorted(
            {
                observation.direction
                for observation in data.provider_observations
                if observation.account_identity == account_identity
                and observation.supply_point_identity == point_identity
            },
            key=lambda direction: direction.value,
        )
    )


def _previous_local_month_start(now: datetime) -> datetime:
    local_month = now.astimezone(TOKYO).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if local_month.month == 1:
        previous = local_month.replace(
            year=local_month.year - 1,
            month=12,
        )
    else:
        previous = local_month.replace(month=local_month.month - 1)
    return previous.astimezone(UTC)
