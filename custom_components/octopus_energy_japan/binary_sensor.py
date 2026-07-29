"""Reading availability binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ReadingDirection
from .coordinator import (
    OejpDataUpdateCoordinator,
    entity_directions,
    iter_supply_points,
)
from .entity import OejpSupplyPointEntity
from .runtime import OejpRuntimeData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up current and future discovered data-availability entities."""
    runtime = entry.runtime_data
    if not isinstance(runtime, OejpRuntimeData) or runtime.coordinator is None:
        raise PlatformNotReady("OEJP runtime coordinator is unavailable")
    coordinator = runtime.coordinator
    created: set[str] = set()

    @callback
    def add_discovered_entities() -> None:
        entities: list[OejpDataAvailableBinarySensor] = []
        for account in coordinator.accounts:
            for point in iter_supply_points(account):
                for direction in entity_directions(
                    point,
                    coordinator.capabilities,
                ):
                    entity = OejpDataAvailableBinarySensor(
                        coordinator,
                        runtime.identity_secret,
                        account.number,
                        point.id,
                        direction,
                    )
                    if entity.unique_id not in created:
                        created.add(entity.unique_id)
                        entities.append(entity)
        if entities:
            async_add_entities(entities)

    add_discovered_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_discovered_entities))


class OejpDataAvailableBinarySensor(
    OejpSupplyPointEntity,
    BinarySensorEntity,
):
    """Whether a completed interval exists for one direction."""

    def __init__(
        self,
        coordinator: OejpDataUpdateCoordinator,
        identity_secret: str,
        account_id: str,
        supply_point_id: str,
        direction: ReadingDirection,
    ) -> None:
        self._reading_direction = direction
        self._attr_translation_key = f"{direction.value}_data_available"
        super().__init__(
            coordinator,
            identity_secret,
            account_id,
            supply_point_id,
            "data_available",
            direction,
        )

    @property
    def is_on(self) -> bool:
        """Return true only when at least one completed interval is available."""
        value = self.coordinator.data.supply_point_aggregation(
            self._account_id,
            self._supply_point_id,
            self._reading_direction,
        )
        return value is not None and value.latest is not None
