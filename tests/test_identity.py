"""Tests for installation-local OEJP identifiers."""

from __future__ import annotations

from custom_components.octopus_energy_japan.identity import (
    async_get_identity_secret,
    stable_account_identity,
    stable_login_identity,
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
