"""Home Assistant Store persistence tests for ledger partitions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.ledger_store import (
    HomeAssistantLedgerBackend,
)
from homeassistant.core import HomeAssistant

STORAGE_SCOPE = f"supply-point-{'a' * 64}"
OTHER_STORAGE_SCOPE = f"supply-point-{'b' * 64}"


async def test_store_backend_persists_private_index_and_partition(
    hass: HomeAssistant,
) -> None:
    backend = HomeAssistantLedgerBackend(
        hass,
        "entry-1",
        STORAGE_SCOPE,
        save_delay=0,
    )
    payload = {
        "schema_version": 1,
        "partition": "2026-07",
        "records": [],
    }

    await backend.async_save_partition("2026-07", payload)
    await backend.async_save_index({"2026-07"})
    partition_store = backend._partition_store("2026-07")
    assert backend._index_store._atomic_writes is True
    assert partition_store._atomic_writes is True
    assert partition_store._serialize_in_event_loop is False

    reloaded = HomeAssistantLedgerBackend(
        hass,
        "entry-1",
        STORAGE_SCOPE,
        save_delay=0,
    )
    assert await reloaded.async_load_index() == {"2026-07"}
    assert await reloaded.async_load_partition("2026-07") == payload

    await reloaded.async_remove_partition("2026-07")
    assert await reloaded.async_load_partition("2026-07") is None


async def test_store_backend_debounces_latest_payload_and_flushes(
    hass: HomeAssistant,
) -> None:
    backend = HomeAssistantLedgerBackend(
        hass,
        "entry-1",
        STORAGE_SCOPE,
        save_delay=60,
    )
    first = {"partition": "2026-07", "records": [{"value": "1"}]}
    latest = {"partition": "2026-07", "records": [{"value": "2"}]}

    await backend.async_save_partition("2026-07", first)
    await backend.async_save_partition("2026-07", latest)
    await backend.async_save_index({"2026-06"})
    await backend.async_save_index({"2026-06", "2026-07"})

    assert await backend.async_load_partition("2026-07") == latest
    await backend.async_flush()

    reloaded = HomeAssistantLedgerBackend(
        hass,
        "entry-1",
        STORAGE_SCOPE,
        save_delay=0,
    )
    assert await reloaded.async_load_partition("2026-07") == latest
    assert await reloaded.async_load_index() == {"2026-06", "2026-07"}

    await backend.async_flush()


async def test_store_backend_keeps_config_entries_isolated(
    hass: HomeAssistant,
) -> None:
    first = HomeAssistantLedgerBackend(
        hass,
        "entry-1",
        STORAGE_SCOPE,
        save_delay=0,
    )
    second = HomeAssistantLedgerBackend(
        hass,
        "entry-2",
        STORAGE_SCOPE,
        save_delay=0,
    )
    await first.async_save_index({"2026-07"})

    assert await second.async_load_index() == set()


async def test_store_backend_keeps_supply_points_isolated(
    hass: HomeAssistant,
) -> None:
    first = HomeAssistantLedgerBackend(
        hass,
        "entry-1",
        STORAGE_SCOPE,
        save_delay=0,
    )
    second = HomeAssistantLedgerBackend(
        hass,
        "entry-1",
        OTHER_STORAGE_SCOPE,
        save_delay=0,
    )
    await first.async_save_index({"2026-07"})

    assert await second.async_load_index() == set()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"partitions": "2026-07"},
        {"partitions": [1]},
        {"partitions": ["../2026-07"]},
    ],
)
async def test_store_backend_rejects_malformed_index(
    hass: HomeAssistant,
    payload: object,
) -> None:
    backend = HomeAssistantLedgerBackend(
        hass,
        "entry-1",
        STORAGE_SCOPE,
        save_delay=0,
    )
    with (
        patch.object(
            backend._index_store,
            "async_load",
            AsyncMock(return_value=payload),
        ),
        pytest.raises(ValueError),
    ):
        await backend.async_load_index()


def test_store_backend_rejects_invalid_configuration(hass: HomeAssistant) -> None:
    with pytest.raises(ValueError, match="entry_id"):
        HomeAssistantLedgerBackend(hass, "", STORAGE_SCOPE, save_delay=0)
    with pytest.raises(ValueError, match="entry_id"):
        HomeAssistantLedgerBackend(
            hass,
            "../entry",
            STORAGE_SCOPE,
            save_delay=0,
        )
    with pytest.raises(ValueError, match="storage_scope"):
        HomeAssistantLedgerBackend(hass, "entry", "SPIN-raw", save_delay=0)
    with pytest.raises(ValueError, match="save_delay"):
        HomeAssistantLedgerBackend(
            hass,
            "entry",
            STORAGE_SCOPE,
            save_delay=-1,
        )


async def test_store_backend_rejects_unsafe_partition_identifiers(
    hass: HomeAssistant,
) -> None:
    backend = HomeAssistantLedgerBackend(
        hass,
        "entry-1",
        STORAGE_SCOPE,
        save_delay=0,
    )
    with pytest.raises(ValueError):
        await backend.async_save_index({"../2026-07"})
    with pytest.raises(ValueError):
        await backend.async_save_partition("../2026-07", {})
    with pytest.raises(ValueError):
        await backend.async_load_partition("../2026-07")
    with pytest.raises(ValueError):
        await backend.async_remove_partition("../2026-07")
