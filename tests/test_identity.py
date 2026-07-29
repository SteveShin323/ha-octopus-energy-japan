"""Tests for installation-local OEJP identifiers."""

from __future__ import annotations

from custom_components.octopus_energy_japan.identity import (
    async_get_identity_secret,
    stable_account_identity,
)
from homeassistant.core import HomeAssistant


async def test_identity_secret_is_persisted(hass: HomeAssistant) -> None:
    first = await async_get_identity_secret(hass)
    second = await async_get_identity_secret(hass)

    assert first == second
    assert len(bytes.fromhex(first)) == 32


def test_account_identity_is_order_independent_and_secret_specific() -> None:
    secret_a = "00" * 32
    secret_b = "01" * 32

    first = stable_account_identity(secret_a, "A-ACCOUNT")
    repeated = stable_account_identity(secret_a, " A-ACCOUNT ")
    different_installation = stable_account_identity(secret_b, "A-ACCOUNT")
    different_account = stable_account_identity(secret_a, "B-ACCOUNT")

    assert first == repeated
    assert first != different_installation
    assert first != different_account
    assert "A-ACCOUNT" not in first


def test_account_identity_requires_an_account_number() -> None:
    try:
        stable_account_identity("00" * 32, " ")
    except ValueError as err:
        assert str(err) == "An account number is required"
    else:
        raise AssertionError("Expected ValueError")
