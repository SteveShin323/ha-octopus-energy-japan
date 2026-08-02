"""Private Home Assistant checkpoint Store tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.sync_store import (
    HomeAssistantSyncCheckpointBackend,
)
from homeassistant.core import HomeAssistant

ENTRY_ID = "entry-safe_123"
SCOPE = "supply-point-" + "a" * 64


async def test_checkpoint_backend_uses_private_atomic_opaque_store(
    hass: HomeAssistant,
) -> None:
    store = AsyncMock()
    store.async_load.return_value = {"schema_version": 1}
    with patch(
        "custom_components.octopus_energy_japan.sync_store.Store",
        return_value=store,
    ) as store_type:
        backend = HomeAssistantSyncCheckpointBackend(hass, ENTRY_ID, SCOPE)
        assert await backend.async_load() == {"schema_version": 1}
        payload = {"schema_version": 1, "values": ["safe"]}
        await backend.async_save(payload)

    store_type.assert_called_once_with(
        hass,
        1,
        f"octopus_energy_japan.sync.{ENTRY_ID}.{SCOPE}",
        private=True,
        atomic_writes=True,
        serialize_in_event_loop=False,
    )
    store.async_save.assert_awaited_once_with(payload)


@pytest.mark.parametrize(
    ("entry_id", "scope", "message"),
    [
        ("unsafe/entry", SCOPE, "entry_id"),
        (ENTRY_ID, "PRIVATE-SPIN", "opaque"),
    ],
)
def test_checkpoint_backend_rejects_unsafe_storage_names(
    hass: HomeAssistant,
    entry_id: str,
    scope: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        HomeAssistantSyncCheckpointBackend(hass, entry_id, scope)
