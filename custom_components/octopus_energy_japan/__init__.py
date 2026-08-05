"""Octopus Energy Japan integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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
    from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
    from homeassistant.core import callback
    from homeassistant.exceptions import (
        ConfigEntryAuthFailed,
        ConfigEntryNotReady,
        OAuth2TokenRequestError,
        OAuth2TokenRequestReauthError,
        OAuth2TokenRequestTransientError,
    )
    from homeassistant.helpers import config_entry_oauth2_flow
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.util import dt as dt_util

    from .api import (
        AuthenticatedGraphQLClient,
        AuthSession,
        OejpAuthenticationError,
        OejpError,
        OejpGraphQLClient,
        OejpRateLimitError,
        OejpTransportError,
    )
    from .commercial_coordinator import OejpCommercialCoordinator
    from .const import (
        AUTH_METHOD_OAUTH,
        AUTH_METHOD_PASSWORD,
        CONF_AUTH_METHOD,
        DOMAIN,
        OAUTH_AUTH_METHODS,
    )
    from .coordinator import OejpDataUpdateCoordinator
    from .identity import async_get_identity_secret
    from .issues import async_update_issues
    from .oauth import OejpOAuthError, OejpPkceAuthSession
    from .oauth_metadata import (
        OAuthMetadataUnavailableError,
        require_oauth_metadata,
    )
    from .password_auth import OejpPasswordAuthError, OejpPasswordAuthSession
    from .runtime import OejpRuntimeData, async_project_discovered_devices
    from .statistics_runtime import HomeAssistantStatisticsProjector

    client = OejpGraphQLClient(async_get_clientsession(hass))
    auth: AuthSession
    method = entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_OAUTH)

    if method not in (AUTH_METHOD_PASSWORD, *OAUTH_AUTH_METHODS):
        # A method this build does not implement, most likely a downgrade after an
        # entry was created by a newer version. Failing here is honest; treating it as
        # OAuth would silently use the wrong credentials.
        raise ConfigEntryAuthFailed(
            f"Unsupported OEJP authentication method: {method}",
            translation_domain=DOMAIN,
            translation_key="unsupported_auth_method",
            translation_placeholders={"method": str(method)},
        )

    if method == AUTH_METHOD_PASSWORD:
        email = entry.data.get(CONF_EMAIL)
        password = entry.data.get(CONF_PASSWORD)
        if not isinstance(email, str) or not isinstance(password, str):
            raise ConfigEntryAuthFailed(
                "OEJP email and password are no longer stored",
                translation_domain=DOMAIN,
                translation_key="credentials_missing",
            )
        try:
            metadata = require_oauth_metadata()
        except OAuthMetadataUnavailableError as err:
            raise ConfigEntryNotReady(
                "OEJP metadata is temporarily unavailable",
                translation_domain=DOMAIN,
                translation_key="metadata_unavailable",
            ) from err
        auth = OejpPasswordAuthSession(
            hass,
            entry,
            client,
            email=email,
            password=password,
            scheme=metadata.authorization_scheme.value,
        )
        try:
            await auth.async_get_authorization_header()
        except OejpPasswordAuthError as err:
            # The stored credential no longer works, or OEJP stopped honouring
            # password login. Retrying cannot fix either, so ask the user.
            raise ConfigEntryAuthFailed(
                "OEJP rejected the stored email and password",
                translation_domain=DOMAIN,
                translation_key="credentials_rejected",
            ) from err
        except (OejpRateLimitError, OejpTransportError) as err:
            raise ConfigEntryNotReady(
                "OEJP sign-in is temporarily unavailable",
                translation_domain=DOMAIN,
                translation_key="sign_in_unavailable",
            ) from err
    else:
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
            raise ConfigEntryNotReady(
                "OEJP OAuth implementation is temporarily unavailable",
                translation_domain=DOMAIN,
                translation_key="oauth_implementation_unavailable",
            ) from err

        auth = OejpPkceAuthSession(hass, entry, implementation, metadata)
        try:
            await auth.async_get_authorization_header()
        except OAuth2TokenRequestReauthError as err:
            raise ConfigEntryAuthFailed(
                "OEJP OAuth authorization must be renewed",
                translation_domain=DOMAIN,
                translation_key="reauth_required",
            ) from err
        except OAuth2TokenRequestTransientError as err:
            raise ConfigEntryNotReady(
                "OEJP OAuth server is temporarily unavailable",
                translation_domain=DOMAIN,
                translation_key="oauth_server_unavailable",
            ) from err
        except OAuth2TokenRequestError as err:
            raise ConfigEntryNotReady(
                "OEJP OAuth token request failed",
                translation_domain=DOMAIN,
                translation_key="oauth_token_request_failed",
            ) from err
        except OejpOAuthError as err:
            raise ConfigEntryAuthFailed(
                "OEJP OAuth token is invalid",
                translation_domain=DOMAIN,
                translation_key="oauth_token_invalid",
            ) from err

    authenticated_client = AuthenticatedGraphQLClient(client, auth)
    try:
        accounts, capabilities = await _async_discover_state(authenticated_client)
    except OejpAuthenticationError as err:
        raise ConfigEntryAuthFailed(
            "OEJP OAuth authorization must be renewed",
            translation_domain=DOMAIN,
            translation_key="reauth_required",
        ) from err
    except (OejpRateLimitError, OejpTransportError) as err:
        raise ConfigEntryNotReady(
            "OEJP discovery is temporarily unavailable",
            translation_domain=DOMAIN,
            translation_key="discovery_unavailable",
        ) from err
    except OejpError as err:
        raise ConfigEntryNotReady(
            "OEJP resource discovery failed",
            translation_domain=DOMAIN,
            translation_key="discovery_failed",
        ) from err

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

    commercial_coordinator = OejpCommercialCoordinator(
        hass,
        entry,
        authenticated_client,
        accounts,
    )

    def tariff_for(account_id: str, supply_point_id: str) -> Any:
        """Look up the tariff the commercial coordinator last read.

        Indirection rather than a value, because the tariff arrives on a twelve-hour
        cadence while statistics are projected every thirty minutes. A cost series simply
        does not appear until the first commercial refresh has run.
        """
        data = commercial_coordinator.data
        return data.tariff(account_id, supply_point_id) if data is not None else None

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
            tariff_lookup=tariff_for,
        ),
    )
    runtime.coordinator = coordinator
    runtime.commercial_coordinator = commercial_coordinator
    entry.runtime_data = runtime
    try:
        # Devices first: the statistics published during the first refresh take their
        # names from the supply-point devices, so those have to exist by then or the
        # Energy dashboard shows an identity digest until the next refresh.
        async_project_discovered_devices(hass, entry, runtime)
        await coordinator.async_config_entry_first_refresh()
        commercial_coordinator.set_accounts(coordinator.accounts)
        entry.async_on_unload(
            coordinator.async_add_listener(
                lambda: commercial_coordinator.set_accounts(coordinator.accounts)
            )
        )

        @callback
        def refresh_issues() -> None:
            commercial = commercial_coordinator.data
            async_update_issues(
                hass,
                entry.entry_id,
                coordinator.data,
                commercial,
                dt_util.utcnow(),
            )

        entry.async_on_unload(coordinator.async_add_listener(refresh_issues))
        entry.async_on_unload(commercial_coordinator.async_add_listener(refresh_issues))
        refresh_issues()

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await coordinator.async_start_background_sync()
        # Optional commercial operations must never delay setup or compete with
        # the first consumption refresh. Arming the debouncer staggers them
        # behind entity creation without blocking on the additional queries.
        await commercial_coordinator.async_request_refresh()
    except BaseException:
        # Platform forwarding can allocate listeners before it fails. Unload is
        # intentionally best-effort so cleanup never hides the setup failure.
        try:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        except Exception:
            _LOGGER.exception("Unable to unload partially set up OEJP platforms")
        await commercial_coordinator.async_shutdown()
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
    if isinstance(runtime, OejpRuntimeData) and runtime.commercial_coordinator is not None:
        await runtime.commercial_coordinator.async_shutdown()
    entry.runtime_data = None
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Best-effort revoke the authorization this entry holds, when it is removed."""
    from homeassistant.helpers import config_entry_oauth2_flow

    from .const import AUTH_METHOD_OAUTH, CONF_AUTH_METHOD, OAUTH_AUTH_METHODS
    from .issues import async_clear_issues
    from .oauth import OejpOAuthRevocationError, OejpPkceAuthSession
    from .oauth_metadata import (
        OAuthMetadataUnavailableError,
        require_oauth_metadata,
    )

    # Repair issues outlive a reload on purpose, so removal is what clears them.
    async_clear_issues(hass, entry.entry_id)

    # Home Assistant deletes the entry's own data, but nothing else. The ledger, its
    # partition files, and the sync checkpoints live in their own stores keyed by entry
    # id, and they hold the account number, the supply-point number, and every stored
    # reading. Removing the integration is the user asking for that to be gone, and the
    # documentation has always said it is, so delete it here.
    await _async_purge_stored_data(hass, entry)

    if entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_OAUTH) not in OAUTH_AUTH_METHODS:
        # Only an OAuth grant can be revoked. For the password method there is nothing
        # to revoke: `invalidateRefreshToken` is rejected for an account user with
        # `AUTHORIZATION/KT-CT-1111`, confirmed live 2026-08-04.
        # Home Assistant deletes the entry data, taking the local copy of the
        # credential and tokens with it, and the refresh token expires at the provider
        # within seven days of the sign-in that issued it.
        return

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


async def _async_purge_stored_data(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete every store this entry owns, and the shared secret when it is the last.

    Store keys are not enumerable through the helper, so the storage directory is
    scanned for this entry's prefixes. That catches every ledger partition, however
    many months were collected, without needing the index to still be readable.

    The installation secret is shared by all entries, because it is what makes device
    and statistic identities stable across them. It is removed only when no entry
    remains, so removing one of two does not rename the other's entities.
    """
    from pathlib import Path

    from homeassistant.helpers.storage import STORAGE_DIR, Store

    from .const import DOMAIN
    from .identity import IDENTITY_STORAGE_KEY

    prefixes = (
        f"{DOMAIN}.ledger.{entry.entry_id}.",
        f"{DOMAIN}.sync.{entry.entry_id}.",
    )
    directory = Path(hass.config.path(STORAGE_DIR))

    def _matching_keys() -> list[str]:
        if not directory.is_dir():
            return []
        return sorted(
            path.name
            for path in directory.iterdir()
            if path.is_file() and path.name.startswith(prefixes)
        )

    keys = await hass.async_add_executor_job(_matching_keys)
    if not hass.config_entries.async_entries(DOMAIN):
        # `async_remove_entry` runs after the entry is gone from the registry, so an
        # empty list here means this was the last one.
        keys.append(IDENTITY_STORAGE_KEY)

    for key in keys:
        try:
            await Store[dict[str, object]](hass, 1, key).async_remove()
        except OSError:
            _LOGGER.warning("Unable to delete stored OEJP data for %s during removal", key)


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
        OejpGraphQLError,
        OejpQueryValidationError,
        OejpRateLimitError,
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
    except OejpRateLimitError:
        # Incomplete discovery, not an absent capability. Let setup retry rather
        # than recording a permanent verdict from a temporary refusal.
        raise
    except OejpGraphQLError:
        # An application-level refusal means this supply point does not expose
        # generic devices. Confirmed on a real account: KT-CT-7899. Optional
        # topology must never prevent the entry from setting up.
        capabilities = capabilities.replace(
            (Capability.DEVICES, Capability.REGISTERS),
            CapabilityAvailability.UNSUPPORTED,
            "generic_device_discovery_unavailable",
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
