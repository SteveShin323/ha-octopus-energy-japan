"""Application Credentials platform for Octopus Energy Japan."""

from __future__ import annotations

from typing import override

from homeassistant.components.application_credentials import ClientCredential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
    LocalOAuth2ImplementationWithPkce,
)

from .oauth_metadata import OAuthMetadataUnavailableError, require_oauth_metadata


class OejpOAuth2Implementation(LocalOAuth2ImplementationWithPkce):
    """OEJP Authorization Code implementation with PKCE S256."""

    def __init__(
        self,
        hass: HomeAssistant,
        auth_domain: str,
        credential: ClientCredential,
    ) -> None:
        metadata = require_oauth_metadata()
        super().__init__(
            hass,
            auth_domain,
            credential.client_id,
            metadata.authorize_url,
            metadata.token_url,
            credential.client_secret,
            code_verifier_length=128,
        )
        self.metadata = metadata

    @property
    @override
    def extra_authorize_data(self) -> dict[str, str]:
        """Request only provider-confirmed scopes while preserving PKCE."""
        return super().extra_authorize_data | {
            "scope": " ".join(self.metadata.scopes),
        }


async def async_get_auth_implementation(
    hass: HomeAssistant,
    auth_domain: str,
    credential: ClientCredential,
) -> OejpOAuth2Implementation:
    """Return the OEJP public-client PKCE implementation."""
    try:
        return OejpOAuth2Implementation(hass, auth_domain, credential)
    except OAuthMetadataUnavailableError as err:
        raise ImplementationUnavailableError(str(err)) from err
