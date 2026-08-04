"""Tests for OEJP PKCE Application Credentials."""

from unittest.mock import patch

import pytest
from custom_components.octopus_energy_japan.application_credentials import (
    OejpOAuth2Implementation,
    async_get_auth_implementation,
)
from custom_components.octopus_energy_japan.oauth_metadata import (
    AuthorizationHeaderScheme,
    OejpOAuthMetadata,
)
from homeassistant.components.application_credentials import ClientCredential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
)

METADATA = OejpOAuthMetadata(
    issuer="https://auth.example.test",
    authorize_url="https://auth.example.test/authorize",
    token_url="https://auth.example.test/token",
    scopes=("openid", "account:read"),
    authorization_scheme=AuthorizationHeaderScheme.BEARER,
)


async def test_application_credentials_create_pkce_public_client(
    hass: HomeAssistant,
) -> None:
    credential = ClientCredential("public-client", "")
    with patch(
        "custom_components.octopus_energy_japan.application_credentials.require_oauth_metadata",
        return_value=METADATA,
    ):
        implementation = await async_get_auth_implementation(hass, "local", credential)

    assert isinstance(implementation, OejpOAuth2Implementation)
    assert implementation.client_id == "public-client"
    assert implementation.client_secret == ""
    assert implementation.extra_authorize_data["scope"] == "openid account:read"
    assert implementation.extra_authorize_data["code_challenge_method"] == "S256"
    assert implementation.metadata is METADATA


async def test_application_credentials_use_published_metadata_by_default(
    hass: HomeAssistant,
) -> None:
    """The provider publishes its endpoints, so only a client ID is still needed."""
    implementation = await async_get_auth_implementation(
        hass,
        "local",
        ClientCredential("public-client", ""),
    )

    assert implementation.metadata.token_url == "https://auth.oejp-kraken.energy/token/"
    assert "openid" in implementation.extra_authorize_data["scope"]
    assert implementation.extra_authorize_data["code_challenge_method"] == "S256"


async def test_application_credentials_fail_closed_without_confirmed_metadata(
    hass: HomeAssistant,
) -> None:
    with (
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.PRODUCTION_OAUTH_METADATA",
            None,
        ),
        pytest.raises(ImplementationUnavailableError),
    ):
        await async_get_auth_implementation(
            hass,
            "local",
            ClientCredential("public-client", ""),
        )
