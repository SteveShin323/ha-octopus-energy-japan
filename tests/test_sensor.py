"""Tests for the OEJP sensor platform."""

from __future__ import annotations

from unittest.mock import Mock

from custom_components.octopus_energy_japan.sensor import async_setup_entry
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_sensor_platform_setup_is_empty_until_runtime_entities_exist(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain="octopus_energy_japan")
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    add_entities.assert_not_called()
