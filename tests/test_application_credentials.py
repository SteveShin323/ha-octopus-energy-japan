"""Tests for OEJP PKCE Application Credentials."""

from unittest.mock import patch

import pytest
from custom_components.octopus_energy_japan.application_credentials import (
    OejpOAuth2Implementation,
    async_built_in_implementation,
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


async def test_the_built_in_client_is_a_public_one(hass: HomeAssistant) -> None:
    """No secret, because a public client has none and Home Assistant omits an empty one."""
    with patch(
        "custom_components.octopus_energy_japan.application_credentials.OEJP_OAUTH_CLIENT_ID",
        "a-public-client",
    ):
        implementation = async_built_in_implementation(hass)

    assert implementation is not None
    assert implementation.client_id == "a-public-client"
    assert implementation.client_secret == ""


async def test_there_is_no_built_in_client_until_one_is_issued(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.octopus_energy_japan.application_credentials.OEJP_OAUTH_CLIENT_ID",
        "",
    ):
        assert async_built_in_implementation(hass) is None


async def test_the_built_in_client_needs_metadata_too(hass: HomeAssistant) -> None:
    """Client ID and endpoints are confirmed separately, so either can be the missing one.

    Registering nothing leaves the sign-in methods unavailable and saying so, which is where
    they already were — better than a half-built implementation aimed at no endpoint.
    """
    with (
        patch(
            "custom_components.octopus_energy_japan.application_credentials.OEJP_OAUTH_CLIENT_ID",
            "a-public-client",
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.PRODUCTION_OAUTH_METADATA",
            None,
        ),
    ):
        assert async_built_in_implementation(hass) is None
