"""Private Home Assistant checkpoint Store tests."""

from __future__ import annotations

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
    backend = HomeAssistantSyncCheckpointBackend(hass, ENTRY_ID, SCOPE)
    assert backend._store._atomic_writes is True
    assert backend._store._serialize_in_event_loop is False
    assert await backend.async_load() is None
    payload = {"schema_version": 1, "values": ["safe"]}
    await backend.async_save(payload)

    reloaded = HomeAssistantSyncCheckpointBackend(hass, ENTRY_ID, SCOPE)
    assert await reloaded.async_load() == payload


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
