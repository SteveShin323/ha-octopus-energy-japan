"""Tests for the OEJP OAuth config flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.api import (
    GraphQLErrorDetail,
    OejpAuthenticationError,
    OejpInvalidResponseError,
    OejpRateLimitError,
    OejpTransportError,
)
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.identity import stable_login_identity
from custom_components.octopus_energy_japan.oauth_metadata import (
    AuthorizationHeaderScheme,
    OejpOAuthMetadata,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

IDENTITY_SECRET = "01" * 32
SUBJECT = "viewer-123"
METADATA = OejpOAuthMetadata(
    issuer="https://auth.example.test",
    authorize_url="https://auth.example.test/authorize",
    token_url="https://auth.example.test/token",
    scopes=("openid", "account:read"),
    authorization_scheme=AuthorizationHeaderScheme.BEARER,
)
TOKEN = {
    "access_token": "access",
    "refresh_token": "refresh",
    "expires_in": 3600,
}


class FakeOAuth2Implementation(config_entry_oauth2_flow.AbstractOAuth2Implementation):
    """Deterministic OAuth implementation for config-flow tests."""

    metadata = METADATA

    @property
    def name(self) -> str:
        return "Test OAuth"

    @property
    def domain(self) -> str:
        return "test"

    async def async_generate_authorize_url(self, flow_id: str) -> str:
        return f"https://auth.example.test/authorize?flow={flow_id}"

    async def async_resolve_external_data(self, external_data: Any) -> dict[str, Any]:
        assert external_data["code"] == "code"
        return dict(TOKEN)

    async def _async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
        return token


async def _complete_oauth_flow(
    hass: HomeAssistant,
    *,
    source: str = config_entries.SOURCE_USER,
    source_data: dict[str, Any] | None = None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    implementation = FakeOAuth2Implementation()
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_implementations",
            AsyncMock(return_value={"test": implementation}),
        ),
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_get_viewer_identity",
            AsyncMock(return_value=SUBJECT),
        ),
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_get_identity_secret",
            AsyncMock(return_value=IDENTITY_SECRET),
        ),
    ):
        context = {"source": source}
        if entry_id is not None:
            context["entry_id"] = entry_id
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context=context,
            data=source_data,
        )
        if source == config_entries.SOURCE_REAUTH:
            assert result["type"] is FlowResultType.FORM
            assert result["step_id"] == "reauth_confirm"
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "pick_implementation"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"implementation": "test"},
        )
        assert result["type"] is FlowResultType.EXTERNAL_STEP
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"code": "code", "state": {"redirect_uri": "https://example.test/callback"}},
        )
        assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE
        return await hass.config_entries.flow.async_configure(result["flow_id"])


async def test_oauth_flow_creates_one_login_scoped_entry(hass: HomeAssistant) -> None:
    result = await _complete_oauth_flow(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Octopus Energy Japan"
    assert result["data"]["token"]["access_token"] == "access"
    assert result["data"]["oauth_issuer"] == METADATA.issuer
    assert result["result"].unique_id == stable_login_identity(
        IDENTITY_SECRET,
        METADATA.issuer,
        SUBJECT,
    )
    assert "email" not in result["data"]
    assert "password" not in result["data"]


async def test_same_oauth_login_is_not_duplicated(hass: HomeAssistant) -> None:
    first = await _complete_oauth_flow(hass)
    second = await _complete_oauth_flow(hass)

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"


async def test_reauth_updates_existing_entry_without_changing_identity(
    hass: HomeAssistant,
) -> None:
    unique_id = stable_login_identity(IDENTITY_SECRET, METADATA.issuer, SUBJECT)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={
            "auth_implementation": "test",
            "oauth_issuer": METADATA.issuer,
            "token": {
                "access_token": "old",
                "refresh_token": "old-refresh",
                "expires_at": 0,
            },
        },
    )
    entry.add_to_hass(hass)

    result = await _complete_oauth_flow(
        hass,
        source=config_entries.SOURCE_REAUTH,
        source_data=dict(entry.data),
        entry_id=entry.entry_id,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == unique_id
    assert entry.data["token"]["access_token"] == "access"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize(
    ("error", "reason"),
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
            "oauth_unauthorized",
        ),
        (OejpTransportError("network failed"), "cannot_connect"),
        (
            OejpRateLimitError(
                (
                    GraphQLErrorDetail(
                        message="GraphQL operation failed",
                        error_code="KT-CT-1199",
                    ),
                )
            ),
            "cannot_connect",
        ),
        (OejpInvalidResponseError("invalid"), "oauth_identity_unavailable"),
    ],
)
async def test_identity_validation_errors_abort_safely(
    hass: HomeAssistant,
    error: Exception,
    reason: str,
) -> None:
    implementation = FakeOAuth2Implementation()
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_implementations",
            AsyncMock(return_value={"test": implementation}),
        ),
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_get_viewer_identity",
            AsyncMock(side_effect=error),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"implementation": "test"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"code": "code", "state": {"redirect_uri": "https://example.test/callback"}},
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
