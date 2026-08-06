"""Consumption, timestamp, delay, and lifecycle sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    MAX_LENGTH_STATE_STATE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .aggregation import SupplyPointAggregation
from .api import (
    AccountCommercialSnapshot,
    CommercialAvailability,
    CommercialFeature,
    ReadingDirection,
    ResourceLifecycle,
)
from .commercial_coordinator import OejpCommercialCoordinator
from .const import CURRENCY_JPY, DOMAIN
from .coordinator import (
    OejpDataUpdateCoordinator,
    entity_directions,
    iter_supply_points,
)
from .entity import OejpAccountEntity, OejpSupplyPointEntity
from .runtime import OejpRuntimeData

type SensorValue = Decimal | datetime | date | float | str | int | None

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class OejpSensorEntityDescription(SensorEntityDescription):
    """Description with a typed aggregate value projection."""

    value_fn: Callable[[SupplyPointAggregation], SensorValue]


@dataclass(frozen=True, kw_only=True)
class OejpCommercialSensorEntityDescription(SensorEntityDescription):
    """Description for one optional account-level commercial projection."""

    feature: CommercialFeature
    value_fn: Callable[[AccountCommercialSnapshot, datetime], SensorValue]


ENERGY_DESCRIPTIONS: tuple[OejpSensorEntityDescription, ...] = (
    OejpSensorEntityDescription(
        key="latest_interval",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # No state class. Home Assistant rejects `measurement` with the energy device
        # class — it logged "state class 'measurement' which is impossible considering
        # device class ('energy')" and told the user to open a bug report here. The
        # alternatives are wrong too: this is one 30-minute total that is replaced by
        # the next, not a running sum, so `total` and `total_increasing` would both
        # invite the recorder to treat consecutive intervals as a cumulative series.
        # Long-term energy history comes from the external statistics this integration
        # publishes, so nothing is lost by leaving this one out of recorder statistics.
        value_fn=lambda value: value.latest.energy_kwh if value.latest is not None else None,
    ),
    OejpSensorEntityDescription(
        key="today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda value: value.today.energy_kwh if value.today.complete else None,
    ),
    OejpSensorEntityDescription(
        key="yesterday",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda value: value.yesterday.energy_kwh if value.yesterday.complete else None,
    ),
    OejpSensorEntityDescription(
        key="this_week",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda value: value.this_week.energy_kwh if value.this_week.complete else None,
    ),
    OejpSensorEntityDescription(
        key="this_month",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda value: value.this_month.energy_kwh if value.this_month.complete else None,
    ),
    OejpSensorEntityDescription(
        key="last_month",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda value: value.last_month.energy_kwh if value.last_month.complete else None,
    ),
    OejpSensorEntityDescription(
        key="latest_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda value: value.latest_reading_end,
    ),
    OejpSensorEntityDescription(
        key="data_delay",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda value: (
            value.data_delay.total_seconds() if value.data_delay is not None else None
        ),
    ),
)


# Provider-issued cost is intentionally absent here. Every projection of OEJP
# `costEstimate` stays unpublished until the currency, permission, interval
# coverage, and correction semantics are confirmed. See
# `docs/API_CONTRACTS.md`.
COMMERCIAL_DESCRIPTIONS: tuple[OejpCommercialSensorEntityDescription, ...] = (
    OejpCommercialSensorEntityDescription(
        key="account_status",
        feature=CommercialFeature.OVERVIEW,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot, _now: (
            snapshot.overview.status if snapshot.overview is not None else None
        ),
    ),
    OejpCommercialSensorEntityDescription(
        key="current_product",
        feature=CommercialFeature.AGREEMENTS,
        value_fn=lambda snapshot, now: _current_product_name(snapshot, now),
    ),
    OejpCommercialSensorEntityDescription(
        key="agreement_start",
        feature=CommercialFeature.AGREEMENTS,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda snapshot, now: _current_agreement_start(snapshot, now),
    ),
    OejpCommercialSensorEntityDescription(
        key="agreement_end",
        feature=CommercialFeature.AGREEMENTS,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda snapshot, now: _current_agreement_end(snapshot, now),
    ),
    OejpCommercialSensorEntityDescription(
        key="account_balance",
        feature=CommercialFeature.OVERVIEW,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_JPY,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot, _now: (
            snapshot.overview.balance_minor if snapshot.overview is not None else None
        ),
    ),
    OejpCommercialSensorEntityDescription(
        key="overdue_balance",
        feature=CommercialFeature.OVERVIEW,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_JPY,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot, _now: (
            snapshot.overview.overdue_balance_minor if snapshot.overview is not None else None
        ),
    ),
    OejpCommercialSensorEntityDescription(
        key="latest_bill_amount",
        feature=CommercialFeature.BILLING,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_JPY,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot, _now: (
            snapshot.latest_bill.gross_amount_minor if snapshot.latest_bill is not None else None
        ),
    ),
    OejpCommercialSensorEntityDescription(
        key="latest_bill_issued",
        feature=CommercialFeature.BILLING,
        device_class=SensorDeviceClass.DATE,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot, _now: (
            snapshot.latest_bill.issued_date if snapshot.latest_bill is not None else None
        ),
    ),
    OejpCommercialSensorEntityDescription(
        key="latest_bill_due",
        feature=CommercialFeature.BILLING,
        device_class=SensorDeviceClass.DATE,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot, _now: (
            snapshot.latest_bill.due_date if snapshot.latest_bill is not None else None
        ),
    ),
    OejpCommercialSensorEntityDescription(
        key="latest_transaction_amount",
        feature=CommercialFeature.BILLING,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_JPY,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot, _now: (
            snapshot.latest_transaction.amount_minor
            if snapshot.latest_transaction is not None
            else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up current and future discovered OEJP sensors."""
    runtime = entry.runtime_data
    if not isinstance(runtime, OejpRuntimeData) or runtime.coordinator is None:
        raise PlatformNotReady(
            "OEJP runtime coordinator is unavailable",
            translation_domain=DOMAIN,
            translation_key="coordinator_unavailable",
        )
    coordinator = runtime.coordinator
    commercial_coordinator = runtime.commercial_coordinator
    created: set[str] = set()

    @callback
    def add_discovered_entities() -> None:
        entities: list[SensorEntity] = []
        for account in coordinator.accounts:
            for point in iter_supply_points(account):
                status = OejpSupplyPointStatusSensor(
                    coordinator,
                    runtime.identity_secret,
                    account.number,
                    point.id,
                )
                for describing in (
                    status,
                    OejpSupplyPointReadingDaySensor(
                        coordinator,
                        runtime.identity_secret,
                        account.number,
                        point.id,
                    ),
                    OejpHistoryCollectedFromSensor(
                        coordinator,
                        runtime.identity_secret,
                        account.number,
                        point.id,
                    ),
                    OejpSupplyPointAddressSensor(
                        coordinator,
                        runtime.identity_secret,
                        account.number,
                        point.id,
                    ),
                ):
                    describing_unique_id = _required_unique_id(describing)
                    if describing_unique_id not in created:
                        created.add(describing_unique_id)
                        entities.append(describing)
                for direction in entity_directions(
                    coordinator.data,
                    runtime.identity_secret,
                    account.number,
                    point.id,
                ):
                    for description in ENERGY_DESCRIPTIONS:
                        entity = OejpConsumptionSensor(
                            coordinator,
                            runtime.identity_secret,
                            account.number,
                            point.id,
                            direction,
                            description,
                        )
                        entity_unique_id = _required_unique_id(entity)
                        if entity_unique_id not in created:
                            created.add(entity_unique_id)
                            entities.append(entity)
        if entities:
            async_add_entities(entities)

    @callback
    def add_commercial_entities() -> None:
        if commercial_coordinator is None:
            return
        entities: list[SensorEntity] = []
        for account in coordinator.accounts:
            for description in COMMERCIAL_DESCRIPTIONS:
                entity = OejpAccountCommercialSensor(
                    commercial_coordinator,
                    runtime.identity_secret,
                    account.number,
                    description,
                )
                entity_unique_id = _required_unique_id(entity)
                if entity_unique_id not in created:
                    created.add(entity_unique_id)
                    entities.append(entity)
        if entities:
            async_add_entities(entities)

    add_discovered_entities()
    add_commercial_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_discovered_entities))
    if commercial_coordinator is not None:
        entry.async_on_unload(commercial_coordinator.async_add_listener(add_commercial_entities))


def _required_unique_id(entity: SensorEntity) -> str:
    unique_id = entity.unique_id
    if unique_id is None:
        raise RuntimeError("OEJP entity was created without a unique ID")
    return unique_id


class OejpConsumptionSensor(OejpSupplyPointEntity, SensorEntity):
    """One direction-specific aggregate or source timestamp."""

    entity_description: OejpSensorEntityDescription

    def __init__(
        self,
        coordinator: OejpDataUpdateCoordinator,
        identity_secret: str,
        account_id: str,
        supply_point_id: str,
        direction: ReadingDirection,
        description: OejpSensorEntityDescription,
    ) -> None:
        self.entity_description = description
        self._reading_direction = direction
        self._attr_translation_key = f"{direction.value}_{description.key}"
        super().__init__(
            coordinator,
            identity_secret,
            account_id,
            supply_point_id,
            description.key,
            direction,
        )

    @property
    def native_value(self) -> SensorValue:
        """Return the deterministic ledger projection."""
        value = self.coordinator.data.supply_point_aggregation(
            self._account_id,
            self._supply_point_id,
            self._reading_direction,
        )
        if value is None:
            return None
        return self.entity_description.value_fn(value)


class OejpSupplyPointStatusSensor(OejpSupplyPointEntity, SensorEntity):
    """Latest discovered lifecycle without exposing provider status text."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [lifecycle.value for lifecycle in ResourceLifecycle]
    _attr_translation_key = "supply_point_status"

    def __init__(
        self,
        coordinator: OejpDataUpdateCoordinator,
        identity_secret: str,
        account_id: str,
        supply_point_id: str,
    ) -> None:
        super().__init__(
            coordinator,
            identity_secret,
            account_id,
            supply_point_id,
            "status",
        )

    @property
    def native_value(self) -> str | None:
        """Return the normalized discovery lifecycle."""
        lifecycle = self.coordinator.data.supply_point_lifecycle(
            self._account_id,
            self._supply_point_id,
        )
        return lifecycle.value if lifecycle is not None else None


class OejpHistoryCollectedFromSensor(OejpSupplyPointEntity, SensorEntity):
    """How far back a requested history walk has reached.

    The honest progress indicator for a job that runs for hours. A percentage would need to
    know where the account's history begins, which is exactly what the walk is discovering, so
    any percentage would be invented. This marches backwards instead, one step per refresh.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "history_collected_from"

    def __init__(
        self,
        coordinator: OejpDataUpdateCoordinator,
        identity_secret: str,
        account_id: str,
        supply_point_id: str,
    ) -> None:
        super().__init__(
            coordinator,
            identity_secret,
            account_id,
            supply_point_id,
            "history_collected_from",
        )

    @property
    def native_value(self) -> datetime | None:
        """Return the oldest instant any direction has walked back to, if one has."""
        return self.coordinator.backfill_cursor(self._supply_point_identity)


class OejpSupplyPointReadingDaySensor(OejpSupplyPointEntity, SensorEntity):
    """The day of the month the provider reads this meter on.

    This is the schedule, not a date. `nextReadingDate` and `nextNextReadingDate` exist and
    are readable, and are deliberately not published: measured against a real account both
    were in the past, so a sensor called "next reading" would have shown a date weeks gone.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "reading_day_of_month"

    def __init__(
        self,
        coordinator: OejpDataUpdateCoordinator,
        identity_secret: str,
        account_id: str,
        supply_point_id: str,
    ) -> None:
        super().__init__(
            coordinator,
            identity_secret,
            account_id,
            supply_point_id,
            "reading_day_of_month",
        )

    @property
    def native_value(self) -> int | None:
        """Return the reading day, or None when the provider did not give a usable one."""
        return self.coordinator.data.supply_point_reading_day(
            self._account_id,
            self._supply_point_id,
        )


class OejpSupplyPointAddressSensor(OejpSupplyPointEntity, SensorEntity):
    """The provider's address for this supply point's property.

    Disabled by default. An address is the one thing that tells a customer with more than
    one property which device is which, and it is theirs to see on their own instance — but
    an enabled entity is recorded, backed up and exposed to voice assistants, so enabling it
    is their decision rather than this integration's.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "supply_point_address"

    def __init__(
        self,
        coordinator: OejpDataUpdateCoordinator,
        identity_secret: str,
        account_id: str,
        supply_point_id: str,
    ) -> None:
        super().__init__(
            coordinator,
            identity_secret,
            account_id,
            supply_point_id,
            "address",
        )

    @property
    def native_value(self) -> str | None:
        """Return the address, truncated to what a Home Assistant state can hold."""
        address = self.coordinator.data.supply_point_address(
            self._account_id,
            self._supply_point_id,
        )
        if address is None:
            return None
        # A state longer than 255 characters is rejected outright, which would make the
        # entity unavailable rather than merely abbreviated.
        return address[:MAX_LENGTH_STATE_STATE]


class OejpAccountCommercialSensor(OejpAccountEntity, SensorEntity):
    """One account-level optional commercial value."""

    entity_description: OejpCommercialSensorEntityDescription

    def __init__(
        self,
        coordinator: OejpCommercialCoordinator,
        identity_secret: str,
        account_id: str,
        description: OejpCommercialSensorEntityDescription,
    ) -> None:
        self.entity_description = description
        self._attr_translation_key = description.key
        super().__init__(coordinator, identity_secret, account_id, description.key)

    @property
    def available(self) -> bool:
        """Expose only data returned by the relevant optional operation."""
        snapshot = self.account_snapshot
        if snapshot is None or not super().available:
            return False
        return snapshot.feature_access(self.entity_description.feature).availability in {
            CommercialAvailability.AVAILABLE,
            CommercialAvailability.PARTIAL,
        }

    @property
    def native_value(self) -> SensorValue:
        """Return one bounded scalar without raw provider identifiers."""
        snapshot = self.account_snapshot
        if snapshot is None:
            return None
        observed_at = snapshot.observed_at or datetime.now(UTC)
        return self.entity_description.value_fn(snapshot, observed_at)


def _current_product_name(snapshot: AccountCommercialSnapshot, at: datetime) -> str | None:
    agreement = snapshot.current_agreement(at)
    if agreement is None or agreement.product is None:
        return None
    return agreement.product.display_name or agreement.product.full_name or agreement.product.code


def _current_agreement_start(
    snapshot: AccountCommercialSnapshot,
    at: datetime,
) -> datetime | None:
    agreement = snapshot.current_agreement(at)
    return agreement.valid_from if agreement is not None else None


def _current_agreement_end(
    snapshot: AccountCommercialSnapshot,
    at: datetime,
) -> datetime | None:
    agreement = snapshot.current_agreement(at)
    return agreement.valid_to if agreement is not None else None
