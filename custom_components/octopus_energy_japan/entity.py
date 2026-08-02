"""Privacy-preserving coordinator entity base classes."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ReadingDirection
from .const import DOMAIN
from .coordinator import OejpDataUpdateCoordinator, SupplyPointKey
from .identity import stable_account_identity, stable_supply_point_identity


class OejpSupplyPointEntity(CoordinatorEntity[OejpDataUpdateCoordinator]):
    """Base entity that never exposes a raw OEJP identifier."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OejpDataUpdateCoordinator,
        identity_secret: str,
        account_id: str,
        supply_point_id: str,
        entity_key: str,
        direction: ReadingDirection | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._account_id = account_id
        self._supply_point_id = supply_point_id
        self._direction = direction
        self._supply_point_key: SupplyPointKey = (
            account_id,
            supply_point_id,
        )
        self._account_identity = stable_account_identity(identity_secret, account_id)
        identity = stable_supply_point_identity(
            identity_secret,
            account_id,
            supply_point_id,
        )
        direction_key = direction.value if direction is not None else "all"
        self._attr_unique_id = f"{identity}-{direction_key}-{entity_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity)},
        )
        self._supply_point_identity = identity

    @property
    def available(self) -> bool:
        """Retain disappeared entities while marking them unavailable."""
        return (
            super().available
            and self.coordinator.data is not None
            and self._supply_point_key in self.coordinator.data.present_supply_points
            and self._supply_point_key in self.coordinator.data.enabled_supply_points
            and (
                self._direction is None
                or (
                    (
                        status := self.coordinator.data.direction_status(
                            self._account_identity,
                            self._supply_point_identity,
                            self._direction,
                        )
                    )
                    is not None
                    and status.queryable
                )
            )
        )
