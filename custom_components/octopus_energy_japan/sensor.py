"""Consumption, timestamp, delay, and lifecycle sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .aggregation import SupplyPointAggregation
from .api import ReadingDirection, ResourceLifecycle
from .coordinator import (
    OejpDataUpdateCoordinator,
    entity_directions,
    iter_supply_points,
)
from .entity import OejpSupplyPointEntity
from .runtime import OejpRuntimeData

type SensorValue = Decimal | datetime | float | None


@dataclass(frozen=True, kw_only=True)
class OejpSensorEntityDescription(SensorEntityDescription):
    """Description with a typed aggregate value projection."""

    value_fn: Callable[[SupplyPointAggregation], SensorValue]


ENERGY_DESCRIPTIONS: tuple[OejpSensorEntityDescription, ...] = (
    OejpSensorEntityDescription(
        key="latest_interval",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda value: value.latest.energy_kwh if value.latest is not None else None,
    ),
    OejpSensorEntityDescription(
        key="today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda value: value.today.energy_kwh,
    ),
    OejpSensorEntityDescription(
        key="yesterday",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda value: value.yesterday.energy_kwh,
    ),
    OejpSensorEntityDescription(
        key="this_week",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda value: value.this_week.energy_kwh,
    ),
    OejpSensorEntityDescription(
        key="this_month",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda value: value.this_month.energy_kwh,
    ),
    OejpSensorEntityDescription(
        key="last_month",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda value: value.last_month.energy_kwh,
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up current and future discovered OEJP sensors."""
    runtime = entry.runtime_data
    if not isinstance(runtime, OejpRuntimeData) or runtime.coordinator is None:
        raise PlatformNotReady("OEJP runtime coordinator is unavailable")
    coordinator = runtime.coordinator
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
                status_unique_id = _required_unique_id(status)
                if status_unique_id not in created:
                    created.add(status_unique_id)
                    entities.append(status)
                for direction in entity_directions(
                    point,
                    coordinator.capabilities,
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

    add_discovered_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_discovered_entities))


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
