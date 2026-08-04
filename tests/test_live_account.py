"""Live end-to-end check of the poll path, against the real account.

The triage table replaced the exception ladder inside `_async_update_data`, so this drives a
real setup and a real refresh and asserts the coordinator reaches a healthy state: every
direction queryable, none stale, no error class recorded.

Skipped unless `OEJP_EMAIL` and `OEJP_PASSWORD` are both set, so CI never runs it and no
credential is stored here. To run it against your own account:

    OEJP_EMAIL=... OEJP_PASSWORD=... python -m pytest tests/test_live_account.py -s

Prints counts and states only — never a reading value, an account number, or a supply point
number. It exists because three separate network blocks have to come off for a real request
to leave the test harness, and rediscovering which ones has twice cost more than the test.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from custom_components.octopus_energy_japan.const import (
    AUTH_METHOD_PASSWORD,
    CONF_AUTH_METHOD,
    DOMAIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

pytestmark = pytest.mark.skipif(
    not (os.environ.get("OEJP_EMAIL") and os.environ.get("OEJP_PASSWORD")),
    reason="live credentials not supplied",
)


@contextlib.asynccontextmanager
async def _real_network() -> AsyncIterator[None]:
    """Undo the harness's three separate network blocks for this one test.

    Nothing about the integration changes: it still calls `async_get_clientsession(hass)`
    exactly as it does live. Only what that returns is replaced, because Home Assistant's
    shared session carries a DNS resolver bound to another event loop.
    """
    import socket

    import aiohttp
    import pytest_socket
    from pytest_homeassistant_custom_component import plugins

    # `pytest-socket` refuses connections, and the Home Assistant test plugin separately
    # replaces `socket.getaddrinfo` with one that rejects any hostname. The plugin keeps the
    # original, so it is restored from there rather than guessed at.
    pytest_socket.enable_socket()
    pytest_socket._remove_restrictions()
    patched_getaddrinfo = socket.getaddrinfo
    socket.getaddrinfo = plugins._real_getaddrinfo
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    )
    try:
        # `config_flow` imports the helper at module level; `__init__` imports it inside
        # the setup function, so patching the source covers that one.
        with (
            patch(
                "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                return_value=session,
            ),
            patch(
                "custom_components.octopus_energy_japan.config_flow.async_get_clientsession",
                return_value=session,
            ),
        ):
            yield
    finally:
        await session.close()
        socket.getaddrinfo = patched_getaddrinfo


async def test_a_real_poll_reaches_a_healthy_coordinator_state(hass: HomeAssistant) -> None:
    async with _real_network():
        await _drive_a_real_poll(hass)


async def _drive_a_real_poll(hass: HomeAssistant) -> None:
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": AUTH_METHOD_PASSWORD}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"email": os.environ["OEJP_EMAIL"], "password": os.environ["OEJP_PASSWORD"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data[CONF_AUTH_METHOD] == AUTH_METHOD_PASSWORD
    print(f"=== entry state: {entry.state} ===")

    coordinator = entry.runtime_data.coordinator
    assert coordinator is not None
    assert coordinator.last_update_success, "the first real refresh failed"

    data = coordinator.data
    statuses = data.direction_statuses
    print(f"=== directions: {len(statuses)} ===")
    for status in statuses:
        print(
            f"   queryable={status.queryable} stale={status.stale} "
            f"error_class={status.error_class} "
            f"has_success={status.last_success_at is not None} "
            f"has_coverage={status.coverage_start_at is not None}"
        )

    assert statuses, "no direction was attempted"
    # The triage table only records a failure; a healthy poll must record none at all.
    assert all(status.error_class is None for status in statuses)
    assert all(status.queryable for status in statuses)
    assert not any(status.stale for status in statuses)
    assert all(status.last_success_at is not None for status in statuses)

    entities = er.async_get(hass).entities.get_entries_for_config_entry_id(entry.entry_id)
    states = [hass.states.get(e.entity_id) for e in entities if not e.disabled]
    unavailable = [s for s in states if s is not None and s.state == "unavailable"]
    print(f"=== entities: {len(entities)} registered, {len(unavailable)} unavailable ===")
    assert not unavailable

    # A second refresh exercises the poll path again with state already present, which is
    # the path the ladder used to take on every subsequent poll.
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert all(status.error_class is None for status in coordinator.data.direction_statuses)
    print("=== second refresh: healthy ===")
