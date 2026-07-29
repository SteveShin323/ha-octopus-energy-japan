"""Octopus Energy Japan integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Octopus Energy Japan from an OAuth config entry."""
    from homeassistant.exceptions import (
        ConfigEntryAuthFailed,
        ConfigEntryNotReady,
        OAuth2TokenRequestError,
        OAuth2TokenRequestReauthError,
        OAuth2TokenRequestTransientError,
    )
    from homeassistant.helpers import config_entry_oauth2_flow

    from .oauth import OejpOAuthError, OejpPkceAuthSession
    from .oauth_metadata import (
        OAuthMetadataUnavailableError,
        require_oauth_metadata,
    )

    try:
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass,
            entry,
        )
        metadata = require_oauth_metadata()
    except (
        config_entry_oauth2_flow.ImplementationUnavailableError,
        OAuthMetadataUnavailableError,
        ValueError,
    ) as err:
        raise ConfigEntryNotReady("OEJP OAuth implementation is temporarily unavailable") from err

    auth = OejpPkceAuthSession(hass, entry, implementation, metadata)
    try:
        await auth.async_get_authorization_header()
    except OAuth2TokenRequestReauthError as err:
        raise ConfigEntryAuthFailed("OEJP OAuth authorization must be renewed") from err
    except OAuth2TokenRequestTransientError as err:
        raise ConfigEntryNotReady("OEJP OAuth server is temporarily unavailable") from err
    except OAuth2TokenRequestError as err:
        raise ConfigEntryNotReady("OEJP OAuth token request failed") from err
    except OejpOAuthError as err:
        raise ConfigEntryAuthFailed("OEJP OAuth token is invalid") from err

    entry.runtime_data = auth
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        entry.runtime_data = None
    return bool(unloaded)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Best-effort revoke OAuth authorization when an entry is removed."""
    from homeassistant.helpers import config_entry_oauth2_flow

    from .oauth import OejpOAuthRevocationError, OejpPkceAuthSession
    from .oauth_metadata import (
        OAuthMetadataUnavailableError,
        require_oauth_metadata,
    )

    try:
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass,
            entry,
        )
        auth = OejpPkceAuthSession(hass, entry, implementation, require_oauth_metadata())
        await auth.async_revoke()
    except (
        config_entry_oauth2_flow.ImplementationUnavailableError,
        OAuthMetadataUnavailableError,
        OejpOAuthRevocationError,
        ValueError,
    ):
        _LOGGER.warning("Unable to revoke OEJP OAuth authorization during entry removal")
