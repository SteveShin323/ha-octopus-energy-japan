"""Tests for installation-local OEJP identifiers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.identity import (
    async_get_identity_secret,
    stable_account_identity,
    stable_login_identity,
    stable_resource_identity,
    stable_supply_point_identity,
)
from homeassistant.core import HomeAssistant


async def test_identity_secret_is_persisted(hass: HomeAssistant) -> None:
    first = await async_get_identity_secret(hass)
    second = await async_get_identity_secret(hass)

    assert first == second
    assert len(bytes.fromhex(first)) == 32


async def test_invalid_stored_identity_secret_is_replaced(
    hass: HomeAssistant,
) -> None:
    replacement = "02" * 32

    with (
        patch(
            "custom_components.octopus_energy_japan.identity.Store.async_load",
            AsyncMock(return_value={"secret": "not-hex"}),
        ) as load,
        patch(
            "custom_components.octopus_energy_japan.identity.Store.async_save",
            AsyncMock(),
        ) as save,
        patch(
            "custom_components.octopus_energy_japan.identity.secrets.token_hex",
            return_value=replacement,
        ),
    ):
        assert await async_get_identity_secret(hass) == replacement

    load.assert_awaited_once_with()
    save.assert_awaited_once_with({"secret": replacement})


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


def test_resource_identity_is_type_scoped_and_private() -> None:
    secret = "01" * 32

    supply_point = stable_resource_identity(secret, "supply-point", "SPIN-123")

    assert supply_point.startswith("supply-point-")
    assert "SPIN-123" not in supply_point
    assert supply_point != stable_resource_identity(secret, "meter", "SPIN-123")


def test_resource_identity_requires_type_and_provider_identifier() -> None:
    for resource_type, provider_id in (("", "id"), ("type", ""), (" ", "id"), ("type", " ")):
        with pytest.raises(ValueError, match="resource type and provider identifier"):
            stable_resource_identity("01" * 32, resource_type, provider_id)


def test_supply_point_identity_is_account_scoped_and_private() -> None:
    secret = "01" * 32
    identity = stable_supply_point_identity(secret, "ACCOUNT-A", "POINT-1")

    assert identity == stable_supply_point_identity(
        secret,
        " ACCOUNT-A ",
        " POINT-1 ",
    )
    assert identity != stable_supply_point_identity(secret, "ACCOUNT-B", "POINT-1")
    assert identity != stable_supply_point_identity(secret, "ACCOUNT-A", "POINT-2")
    assert "ACCOUNT-A" not in identity
    assert "POINT-1" not in identity


@pytest.mark.parametrize(
    ("account", "supply_point"),
    [("", "point"), (" ", "point"), ("account", ""), ("account", " ")],
)
def test_supply_point_identity_requires_both_scopes(
    account: str,
    supply_point: str,
) -> None:
    with pytest.raises(ValueError, match="Account and supply-point"):
        stable_supply_point_identity("01" * 32, account, supply_point)


def test_login_identity_is_private_stable_and_canonical() -> None:
    secret = "01" * 32
    identity = stable_login_identity(secret, "https://auth.example.test/", "viewer-123")

    assert identity == stable_login_identity(
        secret,
        "https://auth.example.test",
        "viewer-123",
    )
    assert identity.startswith("login-")
    assert "viewer-123" not in identity
    assert identity != stable_login_identity(
        "02" * 32,
        "https://auth.example.test",
        "viewer-123",
    )


def test_login_identity_requires_issuer_and_subject() -> None:
    for issuer, subject in (
        ("", "viewer"),
        ("https://auth.example.test", ""),
        (" ", "viewer"),
        ("https://auth.example.test", " "),
    ):
        try:
            stable_login_identity("01" * 32, issuer, subject)
        except ValueError:
            continue
        raise AssertionError("Expected ValueError")
