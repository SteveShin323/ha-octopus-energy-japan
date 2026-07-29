"""Tests for the OEJP Home Assistant config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.api import (
    GraphQLErrorDetail,
    OejpAccount,
    OejpAuthenticationError,
    OejpAuthorizationError,
    OejpInvalidResponseError,
    OejpRateLimitError,
    OejpToken,
    OejpTransportError,
)
from custom_components.octopus_energy_japan.const import CONF_ACCOUNT_NUMBER, DOMAIN
from custom_components.octopus_energy_japan.identity import stable_account_identity
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

USER_INPUT = {
    CONF_EMAIL: " USER@Example.COM ",
    CONF_PASSWORD: "password",
}
ACCOUNTS = (
    OejpAccount(number="A-ACCOUNT", status="ACTIVE"),
    OejpAccount(number="B-ACCOUNT", status="ACTIVE"),
)
IDENTITY_SECRET = "01" * 32


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_valid_credentials_create_discovery_based_entry(
    hass: HomeAssistant,
) -> None:
    with (
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_obtain_token",
            AsyncMock(return_value=OejpToken(access_token="access")),
        ) as obtain_token,
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_discover_accounts",
            AsyncMock(return_value=ACCOUNTS[:1]),
        ) as discover_accounts,
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_get_identity_secret",
            AsyncMock(return_value=IDENTITY_SECRET),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data=USER_INPUT,
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"] == {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "password",
        CONF_ACCOUNT_NUMBER: "A-ACCOUNT",
    }
    entry = result["result"]
    assert entry.unique_id == stable_account_identity(
        IDENTITY_SECRET,
        "A-ACCOUNT",
    )
    obtain_token.assert_awaited_once()
    discover_accounts.assert_awaited_once()


async def test_multiple_accounts_require_selection(hass: HomeAssistant) -> None:
    with (
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_obtain_token",
            AsyncMock(return_value=OejpToken(access_token="access")),
        ),
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_discover_accounts",
            AsyncMock(return_value=ACCOUNTS),
        ),
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_get_identity_secret",
            AsyncMock(return_value=IDENTITY_SECRET),
        ),
    ):
        select_result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data=USER_INPUT,
        )
        result = await hass.config_entries.flow.async_configure(
            select_result["flow_id"],
            {CONF_ACCOUNT_NUMBER: "B-ACCOUNT"},
        )

    assert select_result["type"] is FlowResultType.FORM
    assert select_result["step_id"] == "account"
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCOUNT_NUMBER] == "B-ACCOUNT"
    assert result["result"].unique_id == stable_account_identity(
        IDENTITY_SECRET,
        "B-ACCOUNT",
    )


async def test_same_account_with_changed_email_is_not_duplicated(
    hass: HomeAssistant,
) -> None:
    async def run_flow(email: str) -> dict[str, object]:
        with (
            patch(
                "custom_components.octopus_energy_japan.config_flow.async_obtain_token",
                AsyncMock(return_value=OejpToken(access_token="access")),
            ),
            patch(
                "custom_components.octopus_energy_japan.config_flow.async_discover_accounts",
                AsyncMock(return_value=ACCOUNTS[:1]),
            ),
            patch(
                "custom_components.octopus_energy_japan.config_flow.async_get_identity_secret",
                AsyncMock(return_value=IDENTITY_SECRET),
            ),
        ):
            return await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER},
                data={CONF_EMAIL: email, CONF_PASSWORD: "password"},
            )

    first = await run_flow("first@example.com")
    second = await run_flow("second@example.com")

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"


async def test_different_accounts_from_same_email_can_be_configured(
    hass: HomeAssistant,
) -> None:
    async def run_flow(account_number: str) -> dict[str, object]:
        with (
            patch(
                "custom_components.octopus_energy_japan.config_flow.async_obtain_token",
                AsyncMock(return_value=OejpToken(access_token="access")),
            ),
            patch(
                "custom_components.octopus_energy_japan.config_flow.async_discover_accounts",
                AsyncMock(return_value=ACCOUNTS),
            ),
            patch(
                "custom_components.octopus_energy_japan.config_flow.async_get_identity_secret",
                AsyncMock(return_value=IDENTITY_SECRET),
            ),
        ):
            select_result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER},
                data=USER_INPUT,
            )
            return await hass.config_entries.flow.async_configure(
                select_result["flow_id"],
                {CONF_ACCOUNT_NUMBER: account_number},
            )

    first = await run_flow("A-ACCOUNT")
    second = await run_flow("B-ACCOUNT")

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert second["type"] is FlowResultType.CREATE_ENTRY
    assert first["result"].unique_id != second["result"].unique_id


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (
            OejpAuthenticationError(
                (
                    GraphQLErrorDetail(
                        message="GraphQL operation failed",
                        error_type="AUTHENTICATION",
                    ),
                )
            ),
            "invalid_auth",
        ),
        (OejpTransportError("network failed"), "cannot_connect"),
        (
            OejpRateLimitError(
                (
                    GraphQLErrorDetail(
                        message="GraphQL operation failed",
                        error_type="RATE_LIMIT",
                        error_code="KT-CT-1199",
                    ),
                )
            ),
            "cannot_connect",
        ),
        (
            OejpAuthorizationError(
                (
                    GraphQLErrorDetail(
                        message="GraphQL operation failed",
                        error_type="AUTHORIZATION",
                        error_code="KT-CT-4177",
                    ),
                )
            ),
            "unknown",
        ),
        (OejpInvalidResponseError("invalid response"), "unknown"),
    ],
)
async def test_api_errors_are_mapped_to_form_errors(
    hass: HomeAssistant,
    error: Exception,
    expected_error: str,
) -> None:
    with patch(
        "custom_components.octopus_energy_japan.config_flow.async_obtain_token",
        AsyncMock(side_effect=error),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data=USER_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}


async def test_no_discovered_accounts_returns_form_error(hass: HomeAssistant) -> None:
    with (
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_obtain_token",
            AsyncMock(return_value=OejpToken(access_token="access")),
        ),
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_discover_accounts",
            AsyncMock(return_value=()),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data=USER_INPUT,
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_accounts"}
