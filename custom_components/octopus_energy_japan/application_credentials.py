"""Application Credentials platform for Octopus Energy Japan."""

from __future__ import annotations

from typing import override

from homeassistant.components.application_credentials import ClientCredential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import (
    ImplementationUnavailableError,
    LocalOAuth2ImplementationWithPkce,
)

from .const import DOMAIN
from .oauth_metadata import (
    OEJP_OAUTH_CLIENT_ID,
    OAuthMetadataUnavailableError,
    require_oauth_metadata,
)


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


def async_built_in_implementation(hass: HomeAssistant) -> OejpOAuth2Implementation | None:
    """Return the implementation built from the shipped client ID, when there is one.

    The client identifies this integration, not the customer, so it is the same for every
    installation and belongs in the code. Returns `None` while no client has been issued,
    which is what leaves the OAuth methods unavailable and saying so.

    No secret: Home Assistant omits `client_secret` from the token request when it is empty,
    which is what a public client needs. A credential added by hand still wins — see
    `oauth_metadata.OEJP_OAUTH_CLIENT_ID` for why that escape hatch is kept.
    """
    if not OEJP_OAUTH_CLIENT_ID:
        return None
    try:
        return OejpOAuth2Implementation(
            hass,
            DOMAIN,
            ClientCredential(
                client_id=OEJP_OAUTH_CLIENT_ID,
                client_secret="",
                name="Octopus Energy Japan",
            ),
        )
    except OAuthMetadataUnavailableError:
        # Metadata and the client ID are confirmed separately; without metadata there is
        # nothing to register, and the sign-in methods stay unavailable as they already were.
        return None
