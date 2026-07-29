"""Tests for the OEJP config-entry lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from custom_components.octopus_energy_japan import async_setup_entry, async_unload_entry
from custom_components.octopus_energy_japan.const import DOMAIN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_setup_entry_forwards_sensor_platform(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        AsyncMock(),
    ) as forward:
        assert await async_setup_entry(hass, entry)

    forward.assert_awaited_once_with(entry, ["sensor"])


async def test_unload_entry_unloads_sensor_platform(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    ) as unload:
        assert await async_unload_entry(hass, entry)

    unload.assert_awaited_once_with(entry, ["sensor"])
