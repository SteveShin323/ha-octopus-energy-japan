"""Octopus Energy Japan integration."""

from __future__ import annotations

import asyncio
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
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .api import (
        Capability,
        CapabilityAvailability,
        CapabilitySnapshot,
        OejpAuthenticationError,
        OejpAuthorizationError,
        OejpError,
        OejpGraphQLClient,
        OejpQueryValidationError,
        OejpRateLimitError,
        OejpTransportError,
        async_detect_capabilities,
        async_discover_generic_devices,
        async_discover_resources,
        attach_generic_devices,
    )
    from .identity import async_get_identity_secret
    from .oauth import OejpOAuthError, OejpPkceAuthSession
    from .oauth_metadata import (
        OAuthMetadataUnavailableError,
        require_oauth_metadata,
    )
    from .runtime import OejpRuntimeData, async_project_discovered_devices

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
        authorization_header = await auth.async_get_authorization_header()
    except OAuth2TokenRequestReauthError as err:
        raise ConfigEntryAuthFailed("OEJP OAuth authorization must be renewed") from err
    except OAuth2TokenRequestTransientError as err:
        raise ConfigEntryNotReady("OEJP OAuth server is temporarily unavailable") from err
    except OAuth2TokenRequestError as err:
        raise ConfigEntryNotReady("OEJP OAuth token request failed") from err
    except OejpOAuthError as err:
        raise ConfigEntryAuthFailed("OEJP OAuth token is invalid") from err

    client = OejpGraphQLClient(async_get_clientsession(hass))
    try:
        accounts = await async_discover_resources(client, authorization_header)
    except OejpAuthenticationError as err:
        raise ConfigEntryAuthFailed("OEJP OAuth authorization must be renewed") from err
    except (OejpRateLimitError, OejpTransportError) as err:
        raise ConfigEntryNotReady("OEJP discovery is temporarily unavailable") from err
    except OejpError as err:
        raise ConfigEntryNotReady("OEJP resource discovery failed") from err

    try:
        capabilities = await async_detect_capabilities(client, authorization_header)
    except OejpAuthenticationError as err:
        raise ConfigEntryAuthFailed("OEJP OAuth authorization must be renewed") from err
    except OejpError:
        # Introspection is optional and can be disabled independently of the
        # customer resource API. Providers will probe their operations later.
        capabilities = CapabilitySnapshot()

    if capabilities.availability(Capability.DEVICES) is CapabilityAvailability.SUPPORTED:
        external_identifiers = sorted(
            {
                supply_point.spin or supply_point.id
                for account in accounts
                for property_ in account.properties
                for supply_point in property_.supply_points
            }
        )
        try:
            discovered_devices = await asyncio.gather(
                *(
                    async_discover_generic_devices(
                        client,
                        authorization_header,
                        external_identifier,
                    )
                    for external_identifier in external_identifiers
                )
            )
        except OejpAuthenticationError as err:
            raise ConfigEntryAuthFailed("OEJP OAuth authorization must be renewed") from err
        except OejpAuthorizationError:
            capabilities = capabilities.replace(
                (Capability.DEVICES, Capability.REGISTERS),
                CapabilityAvailability.FORBIDDEN,
                "generic_device_discovery_forbidden",
            )
            discovered_devices = []
        except OejpQueryValidationError:
            capabilities = capabilities.replace(
                (Capability.DEVICES, Capability.REGISTERS),
                CapabilityAvailability.UNSUPPORTED,
                "generic_device_schema_mismatch",
            )
            discovered_devices = []
        except (OejpRateLimitError, OejpTransportError) as err:
            raise ConfigEntryNotReady(
                "OEJP generic device discovery is temporarily unavailable"
            ) from err
        except OejpError as err:
            raise ConfigEntryNotReady("OEJP generic device discovery failed") from err
        else:
            accounts = attach_generic_devices(
                accounts,
                dict(zip(external_identifiers, discovered_devices, strict=True)),
            )

    runtime = OejpRuntimeData(
        auth=auth,
        accounts=accounts,
        capabilities=capabilities,
        identity_secret=await async_get_identity_secret(hass),
    )
    entry.runtime_data = runtime
    async_project_discovered_devices(hass, entry, runtime)
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
