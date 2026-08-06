"""The one thing in this integration a user asks for rather than receives.

Everything else runs on a cadence. Walking a supply point's readings back to the start of its
history takes hours and is worth doing once, so it is a button rather than a setting: a stored
boolean would re-trigger on every reload, and un-ticking to re-tick is a strange way to retry.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import PlatformNotReady, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import OejpDataUpdateCoordinator, iter_supply_points
from .entity import OejpSupplyPointEntity
from .runtime import OejpRuntimeData

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one history button per current and future supply point."""
    runtime = entry.runtime_data
    if not isinstance(runtime, OejpRuntimeData) or runtime.coordinator is None:
        raise PlatformNotReady(
            "OEJP runtime coordinator is unavailable",
            translation_domain=DOMAIN,
            translation_key="coordinator_unavailable",
        )
    coordinator = runtime.coordinator
    created: set[str] = set()

    @callback
    def add_discovered_entities() -> None:
        entities: list[OejpImportHistoryButton] = []
        for account in coordinator.accounts:
            for point in iter_supply_points(account):
                entity = OejpImportHistoryButton(
                    coordinator,
                    runtime.identity_secret,
                    account.number,
                    point.id,
                )
                unique_id = entity.unique_id
                if unique_id is None:
                    raise RuntimeError("OEJP entity was created without a unique ID")
                if unique_id not in created:
                    created.add(unique_id)
                    entities.append(entity)
        if entities:
            async_add_entities(entities)

    add_discovered_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_discovered_entities))


class OejpImportHistoryButton(OejpSupplyPointEntity, ButtonEntity):
    """Ask one supply point to collect everything older than the usual two months."""

    _attr_translation_key = "import_full_history"
    _attr_entity_category = EntityCategory.CONFIG

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
            "import_full_history",
        )

    async def async_press(self) -> None:
        """Start or resume the walk, refusing when no direction can be walked.

        Refusing out loud matters: the legacy reading path answers with the most recent
        31 days however far back it is asked, so a walk over it would collect a month and
        report a complete history.
        """
        await self.coordinator.async_start_history_backfill(self._supply_point_identity)
        if not self.coordinator.has_running_backfill(self._supply_point_identity):
            raise ServiceValidationError(
                "OEJP cannot collect older readings for this supply point",
                translation_domain=DOMAIN,
                translation_key="backfill_unsupported",
            )
