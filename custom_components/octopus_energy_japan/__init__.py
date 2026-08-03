"""Octopus Energy Japan integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import (
        AuthenticatedGraphQLClient,
        CapabilitySnapshot,
        OejpAccount,
    )

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "binary_sensor"]


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
        AuthenticatedGraphQLClient,
        OejpAuthenticationError,
        OejpError,
        OejpGraphQLClient,
        OejpRateLimitError,
        OejpTransportError,
    )
    from .coordinator import OejpDataUpdateCoordinator
    from .identity import async_get_identity_secret
    from .oauth import OejpOAuthError, OejpPkceAuthSession
    from .oauth_metadata import (
        OAuthMetadataUnavailableError,
        require_oauth_metadata,
    )
    from .runtime import OejpRuntimeData, async_project_discovered_devices
    from .statistics_runtime import HomeAssistantStatisticsProjector

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

    client = OejpGraphQLClient(async_get_clientsession(hass))
    authenticated_client = AuthenticatedGraphQLClient(client, auth)
    try:
        accounts, capabilities = await _async_discover_state(authenticated_client)
    except OejpAuthenticationError as err:
        raise ConfigEntryAuthFailed("OEJP OAuth authorization must be renewed") from err
    except (OejpRateLimitError, OejpTransportError) as err:
        raise ConfigEntryNotReady("OEJP discovery is temporarily unavailable") from err
    except OejpError as err:
        raise ConfigEntryNotReady("OEJP resource discovery failed") from err

    identity_secret = await async_get_identity_secret(hass)
    runtime = OejpRuntimeData(
        auth=auth,
        accounts=accounts,
        capabilities=capabilities,
        identity_secret=identity_secret,
    )

    async def load_discovery() -> tuple[
        tuple[OejpAccount, ...],
        CapabilitySnapshot,
    ]:
        return await _async_discover_state(authenticated_client)

    coordinator = OejpDataUpdateCoordinator(
        hass,
        entry,
        authenticated_client,
        accounts,
        capabilities,
        identity_secret,
        load_discovery,
        statistics_projector=HomeAssistantStatisticsProjector(
            hass,
            identity_secret,
        ),
    )
    runtime.coordinator = coordinator
    entry.runtime_data = runtime
    try:
        await coordinator.async_config_entry_first_refresh()
        async_project_discovered_devices(hass, entry, runtime)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await coordinator.async_start_background_sync()
    except BaseException:
        # Platform forwarding can allocate listeners before it fails. Unload is
        # intentionally best-effort so cleanup never hides the setup failure.
        try:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        except Exception:
            _LOGGER.exception("Unable to unload partially set up OEJP platforms")
        await coordinator.async_shutdown_runtime()
        entry.runtime_data = None
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms and durably flush ledger writes."""
    from .runtime import OejpRuntimeData

    runtime = entry.runtime_data
    if isinstance(runtime, OejpRuntimeData) and runtime.coordinator is not None:
        await runtime.coordinator.async_prepare_shutdown()
    unloaded = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )
    if not unloaded:
        if isinstance(runtime, OejpRuntimeData) and runtime.coordinator is not None:
            await runtime.coordinator.async_resume_runtime()
        return False
    if isinstance(runtime, OejpRuntimeData) and runtime.coordinator is not None:
        await runtime.coordinator.async_shutdown_runtime()
    entry.runtime_data = None
    return True


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
        auth = OejpPkceAuthSession(
            hass,
            entry,
            implementation,
            require_oauth_metadata(),
        )
        await auth.async_revoke()
    except (
        config_entry_oauth2_flow.ImplementationUnavailableError,
        OAuthMetadataUnavailableError,
        OejpOAuthRevocationError,
        ValueError,
    ):
        _LOGGER.warning("Unable to revoke OEJP OAuth authorization during entry removal")


async def _async_discover_state(
    client: AuthenticatedGraphQLClient,
) -> tuple[tuple[OejpAccount, ...], CapabilitySnapshot]:
    """Discover strict customer resources plus optional generic topology."""
    from .api import (
        Capability,
        CapabilityAvailability,
        CapabilitySnapshot,
        OejpAuthenticationError,
        OejpAuthorizationError,
        OejpError,
        OejpQueryValidationError,
        async_detect_capabilities,
        async_discover_generic_devices,
        async_discover_resources,
        attach_generic_devices,
    )

    accounts = await async_discover_resources(client)
    try:
        capabilities = await async_detect_capabilities(client)
    except OejpAuthenticationError:
        raise
    except OejpError:
        # Introspection is optional and may be disabled independently.
        capabilities = CapabilitySnapshot()

    if capabilities.availability(Capability.DEVICES) is not CapabilityAvailability.SUPPORTED:
        return accounts, capabilities

    external_identifiers = sorted(
        {
            point.spin or point.id
            for account in accounts
            for property_ in account.properties
            for point in property_.supply_points
        }
    )
    try:
        discovered_devices = []
        for external_identifier in external_identifiers:
            discovered_devices.append(
                await async_discover_generic_devices(
                    client,
                    external_identifier,
                )
            )
    except OejpAuthenticationError:
        raise
    except OejpAuthorizationError:
        capabilities = capabilities.replace(
            (Capability.DEVICES, Capability.REGISTERS),
            CapabilityAvailability.FORBIDDEN,
            "generic_device_discovery_forbidden",
        )
    except OejpQueryValidationError:
        capabilities = capabilities.replace(
            (Capability.DEVICES, Capability.REGISTERS),
            CapabilityAvailability.UNSUPPORTED,
            "generic_device_schema_mismatch",
        )
    else:
        accounts = attach_generic_devices(
            accounts,
            dict(
                zip(
                    external_identifiers,
                    discovered_devices,
                    strict=True,
                )
            ),
        )
    return accounts, capabilities
