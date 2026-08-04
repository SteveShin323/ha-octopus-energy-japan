"""Tests for the OEJP OAuth config flow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.api import (
    CapabilitySnapshot,
    DeviceAuthorization,
    DeviceAuthorizationDeniedError,
    DeviceAuthorizationError,
    DeviceAuthorizationExpiredError,
    DeviceAuthorizationTransientError,
    GraphQLErrorDetail,
    OejpAccount,
    OejpAuthenticationError,
    OejpInvalidResponseError,
    OejpProperty,
    OejpRateLimitError,
    OejpSupplyPoint,
    OejpToken,
    OejpTransportError,
    ResourceLifecycle,
)
from custom_components.octopus_energy_japan.config_flow import OctopusEnergyJapanConfigFlow
from custom_components.octopus_energy_japan.const import (
    CONF_ENABLED_HISTORICAL_RESOURCES,
    DOMAIN,
)
from custom_components.octopus_energy_japan.identity import (
    stable_account_identity,
    stable_login_identity,
)
from custom_components.octopus_energy_japan.oauth_metadata import (
    OEJP_AUTH_ISSUER,
    AuthorizationHeaderScheme,
    OejpOAuthMetadata,
)
from custom_components.octopus_energy_japan.runtime import OejpRuntimeData
from homeassistant import config_entries
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.setup import async_setup_component
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


@pytest.fixture(autouse=True)
def my_home_assistant_enabled(hass: HomeAssistant) -> None:
    """Load `my`, as every default Home Assistant installation does.

    Without it Home Assistant would build a redirect URI OEJP has not registered,
    and the flow refuses to start. That refusal has its own test below.
    """
    hass.config.components.add("my")


async def _choose_method(
    hass: HomeAssistant,
    result: dict[str, Any],
    method: str,
) -> dict[str, Any]:
    """Advance past the login-method menu that now opens every flow."""
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["oauth", "device", "password"]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": method},
    )


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

        result = await _choose_method(hass, result, "oauth")
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
        OEJP_AUTH_ISSUER,
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
    unique_id = stable_login_identity(IDENTITY_SECRET, OEJP_AUTH_ISSUER, SUBJECT)
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
        result = await _choose_method(hass, result, "oauth")
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


async def test_reconfigure_selects_historical_resources(hass: HomeAssistant) -> None:
    historical_id = stable_account_identity(IDENTITY_SECRET, "OLD-ACCOUNT")
    entry = MockConfigEntry(domain=DOMAIN, options={"future_option": "preserved"})
    entry.runtime_data = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=(
            OejpAccount(
                number="OLD-ACCOUNT",
                lifecycle=ResourceLifecycle.HISTORICAL,
                properties=(
                    OejpProperty(
                        id="OLD-PROPERTY",
                        supply_points=(
                            OejpSupplyPoint(
                                id="OLD-SUPPLY",
                                account_number="OLD-ACCOUNT",
                                lifecycle=ResourceLifecycle.HISTORICAL,
                            ),
                        ),
                    ),
                ),
            ),
        ),
        capabilities=CapabilitySnapshot(),
        identity_secret=IDENTITY_SECRET,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENABLED_HISTORICAL_RESOURCES: [historical_id]},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.options[CONF_ENABLED_HISTORICAL_RESOURCES] == [historical_id]
    assert entry.options["future_option"] == "preserved"


async def test_reconfigure_aborts_without_runtime_or_history(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)

    unavailable = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )

    assert unavailable["type"] is FlowResultType.ABORT
    assert unavailable["reason"] == "reconfigure_unavailable"

    entry.runtime_data = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=(),
        capabilities=CapabilitySnapshot(),
        identity_secret=IDENTITY_SECRET,
    )
    no_history = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )

    assert no_history["type"] is FlowResultType.ABORT
    assert no_history["reason"] == "no_historical_resources"


async def test_first_run_without_application_credentials_tells_the_user_where_to_go(
    hass: HomeAssistant,
) -> None:
    """This is today's real first-run experience: no client ID exists yet.

    Home Assistant's `missing_configuration` text points at Application Credentials,
    which is exactly where the user has to act, so the flow must reach that abort
    rather than a bare error.
    """
    await async_setup_component(hass, "application_credentials", {})

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await _choose_method(hass, result, "oauth")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "missing_credentials"


@pytest.mark.parametrize(
    ("data", "metadata", "reason"),
    [
        ({"token": dict(TOKEN)}, None, "oauth_metadata_unavailable"),
        ({}, METADATA, "oauth_metadata_unavailable"),
        ({"token": {"refresh_token": "refresh"}}, METADATA, "oauth_identity_unavailable"),
        ({"token": {"access_token": ""}}, METADATA, "oauth_identity_unavailable"),
    ],
)
async def test_unusable_token_response_aborts_before_calling_the_api(
    hass: HomeAssistant,
    data: dict[str, Any],
    metadata: OejpOAuthMetadata | None,
    reason: str,
) -> None:
    """A token response the flow cannot use must not reach the API.

    Without an access token there is nothing to authenticate with, and without
    metadata the header scheme and issuer are unknown, so neither the request nor
    the resulting unique ID could be formed correctly.

    Home Assistant rejects a malformed token response before this handler runs, so
    these guards are exercised directly. They are the boundary that keeps a future
    change upstream from turning an unusable response into a broken entry.
    """
    implementation = FakeOAuth2Implementation()
    implementation.metadata = metadata
    handler = OctopusEnergyJapanConfigFlow()
    handler.hass = hass
    handler.flow_id = "direct"
    handler.context = {"source": config_entries.SOURCE_USER}
    handler.flow_impl = implementation

    identity = AsyncMock(return_value=SUBJECT)
    with patch(
        "custom_components.octopus_energy_japan.config_flow.async_get_viewer_identity",
        identity,
    ):
        result = await handler.async_oauth_create_entry(data)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    identity.assert_not_awaited()


@pytest.mark.parametrize(
    "source",
    [config_entries.SOURCE_USER, config_entries.SOURCE_REAUTH],
)
async def test_flow_refuses_to_start_without_my_home_assistant(
    hass: HomeAssistant,
    source: str,
) -> None:
    """Only `https://my.home-assistant.io/redirect/oauth` is registered with OEJP.

    Without `my`, Home Assistant would send this instance's own callback URL, and
    the user would meet the provider's unregistered-redirect error part-way through
    sign-in with nothing naming this integration. Refuse first, while the message
    can still explain what to do.
    """
    hass.config.components.remove("my")
    await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass,
        DOMAIN,
        ClientCredential("public-client", ""),
    )
    entry = MockConfigEntry(domain=DOMAIN, data={"auth_implementation": DOMAIN})
    entry.add_to_hass(hass)

    context: dict[str, Any] = {"source": source}
    if source == config_entries.SOURCE_REAUTH:
        context["entry_id"] = entry.entry_id
    result = await hass.config_entries.flow.async_init(DOMAIN, context=context)
    if source == config_entries.SOURCE_REAUTH:
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _choose_method(hass, result, "oauth")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "my_home_assistant_required"


@pytest.mark.usefixtures("current_request_with_host")
async def test_setup_requires_no_user_input_once_a_credential_exists(
    hass: HomeAssistant,
) -> None:
    """Nothing is typed during setup: no API key, account number, or supply point."""
    await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass,
        DOMAIN,
        ClientCredential("public-client", ""),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await _choose_method(hass, result, "oauth")

    # With one credential there is nothing to choose, so Home Assistant goes
    # straight to the provider-hosted sign-in. The user types nothing at all.
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith("https://auth.oejp-kraken.energy/authorize/")
    assert "code_challenge=" in result["url"]
    assert "code_challenge_method=S256" in result["url"]
    assert "client_secret" not in result["url"]
    for scope in ("openid", "view:account-number", "request:consumption-data"):
        assert scope in result["url"]
    # Least privilege: the broad scope is never requested.
    assert "full-customer-access" not in result["url"]


async def _complete_password_flow(
    hass: HomeAssistant,
    *,
    source: str = config_entries.SOURCE_USER,
    entry_id: str | None = None,
    token: OejpToken | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {"source": source}
    if entry_id is not None:
        context["entry_id"] = entry_id
    with (
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_obtain_token",
            AsyncMock(
                return_value=token
                or OejpToken(
                    access_token="legacy-access",
                    refresh_token="legacy-refresh",
                    refresh_expires_at=datetime(2026, 8, 11, tzinfo=UTC),
                )
            ),
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
        result = await hass.config_entries.flow.async_init(DOMAIN, context=context)
        if source == config_entries.SOURCE_REAUTH:
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _choose_method(hass, result, "password")
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "password"
        return await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "person@example.test", "password": "correct horse"},
        )


async def test_password_login_creates_an_entry_holding_the_credential(
    hass: HomeAssistant,
) -> None:
    result = await _complete_password_flow(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["auth_method"] == "password"
    assert data["email"] == "person@example.test"
    # The credential is stored deliberately: the provider's refresh token lasts
    # seven days and renewing does not extend it, so nothing else can sign in again.
    assert data["password"] == "correct horse"
    assert data["access_token"] == "legacy-access"
    assert data["refresh_token"] == "legacy-refresh"
    assert data["refresh_expires_at"] == "2026-08-11T00:00:00+00:00"
    # No OAuth implementation is involved, so nothing may imply one.
    assert "auth_implementation" not in data
    assert "token" not in data


async def test_password_login_needs_no_my_home_assistant(hass: HomeAssistant) -> None:
    """Only the redirect-based method depends on `my`; this one has no redirect."""
    hass.config.components.remove("my")

    result = await _complete_password_flow(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            OejpAuthenticationError(
                (
                    GraphQLErrorDetail(
                        message="GraphQL operation failed",
                        error_type="VALIDATION",
                        error_code="KT-CT-1138",
                    ),
                )
            ),
            "invalid_auth",
        ),
        (OejpTransportError("network failed"), "cannot_connect"),
        (OejpInvalidResponseError("invalid"), "unknown"),
    ],
)
async def test_password_login_reports_failures_on_the_form(
    hass: HomeAssistant,
    error: Exception,
    expected: str,
) -> None:
    """The user must be able to correct a typo without restarting the flow."""
    with patch(
        "custom_components.octopus_energy_japan.config_flow.async_obtain_token",
        AsyncMock(side_effect=error),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        result = await _choose_method(hass, result, "password")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "person@example.test", "password": "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "password"
    assert result["errors"] == {"base": expected}


async def test_every_method_identifies_the_same_login_identically(
    hass: HomeAssistant,
) -> None:
    """This equality is what makes in-place promotion to OAuth possible.

    The identity is scoped to the provider's issuer rather than to the method, so
    one OEJP login owns one entry however it authenticated. Without this, adding
    OAuth later would create a second entry and orphan the stored history.
    """
    oauth = await _complete_oauth_flow(hass)
    assert oauth["type"] is FlowResultType.CREATE_ENTRY
    oauth_unique_id = oauth["result"].unique_id

    await hass.config_entries.async_remove(oauth["result"].entry_id)
    password = await _complete_password_flow(hass)

    assert password["type"] is FlowResultType.CREATE_ENTRY
    assert password["result"].unique_id == oauth_unique_id
    assert oauth_unique_id == stable_login_identity(
        IDENTITY_SECRET,
        OEJP_AUTH_ISSUER,
        SUBJECT,
    )


async def test_promoting_a_password_entry_to_oauth_deletes_the_stored_password(
    hass: HomeAssistant,
) -> None:
    """Reauthentication is the promotion path, and it must not leave the credential."""
    created = await _complete_password_flow(hass)
    entry = created["result"]
    assert entry.data["password"] == "correct horse"

    result = await _complete_oauth_flow(
        hass,
        source=config_entries.SOURCE_REAUTH,
        source_data=dict(entry.data),
        entry_id=entry.entry_id,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["auth_method"] == "oauth"
    assert "password" not in entry.data
    assert "email" not in entry.data
    assert "access_token" not in entry.data
    assert entry.data["token"]["access_token"] == "access"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_moving_an_oauth_entry_to_a_password_login_drops_the_oauth_token(
    hass: HomeAssistant,
) -> None:
    created = await _complete_oauth_flow(hass)
    entry = created["result"]
    assert entry.data["token"]["access_token"] == "access"

    result = await _complete_password_flow(
        hass,
        source=config_entries.SOURCE_REAUTH,
        entry_id=entry.entry_id,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["auth_method"] == "password"
    assert "token" not in entry.data
    assert entry.data["password"] == "correct horse"


async def test_reauthenticating_with_the_same_method_keeps_the_other_entry_data(
    hass: HomeAssistant,
) -> None:
    created = await _complete_password_flow(hass)
    entry = created["result"]
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "future_key": "preserved"},
    )

    result = await _complete_password_flow(
        hass,
        source=config_entries.SOURCE_REAUTH,
        entry_id=entry.entry_id,
        token=OejpToken(access_token="renewed", refresh_token="renewed-refresh"),
    )

    assert result["reason"] == "reauth_successful"
    assert entry.data["future_key"] == "preserved"
    assert entry.data["access_token"] == "renewed"


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
        (OejpInvalidResponseError("invalid"), "oauth_identity_unavailable"),
    ],
)
async def test_password_login_aborts_when_the_viewer_cannot_be_identified(
    hass: HomeAssistant,
    error: Exception,
    reason: str,
) -> None:
    """The credential worked but the account could not be identified.

    This aborts rather than showing a form error, because retyping the password
    cannot change the outcome.
    """
    with (
        patch(
            "custom_components.octopus_energy_japan.config_flow.async_obtain_token",
            AsyncMock(return_value=OejpToken(access_token="legacy-access")),
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
        result = await _choose_method(hass, result, "password")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "person@example.test", "password": "correct horse"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


DEVICE_AUTHORIZATION = DeviceAuthorization(
    device_code="device-code",
    user_code="WXYZ-1234",
    verification_uri="https://auth.example.test/device",
    verification_uri_complete="https://auth.example.test/device?code=WXYZ-1234",
    expires_in=600,
    interval=5,
)
DEVICE_TOKEN = {
    "access_token": "device-access",
    "refresh_token": "device-refresh",
    "expires_in": 3600,
    "token_type": "Bearer",
}


async def _register_credential(hass: HomeAssistant) -> None:
    await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass,
        DOMAIN,
        ClientCredential("public-client", ""),
    )


async def _run_device_flow(
    hass: HomeAssistant,
    *,
    start: Any = None,
    wait: Any = None,
    source: str = config_entries.SOURCE_USER,
    entry_id: str | None = None,
) -> dict[str, Any]:
    client = AsyncMock()
    client.async_start = AsyncMock(
        side_effect=start if isinstance(start, Exception) else None,
        return_value=DEVICE_AUTHORIZATION if start is None else start,
    )
    client.async_wait_for_token = AsyncMock(
        side_effect=wait if isinstance(wait, Exception) else None,
        return_value=DEVICE_TOKEN if wait is None else wait,
    )
    context: dict[str, Any] = {"source": source}
    if entry_id is not None:
        context["entry_id"] = entry_id
    with (
        patch(
            "custom_components.octopus_energy_japan.config_flow.OejpDeviceAuthorizationClient",
            return_value=client,
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
        result = await hass.config_entries.flow.async_init(DOMAIN, context=context)
        if source == config_entries.SOURCE_REAUTH:
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await _choose_method(hass, result, "device")
        while result["type"] is FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
        return result


async def test_device_flow_shows_the_code_then_creates_the_entry(hass: HomeAssistant) -> None:
    """No redirect is involved, so this path works without My Home Assistant."""
    hass.config.components.remove("my")
    await _register_credential(hass)

    result = await _run_device_flow(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["auth_method"] == "device"
    # The token is stored in the shape Home Assistant's OAuth session refreshes from,
    # which means adding `expires_at`: the device grant only returns `expires_in`.
    assert data["token"]["access_token"] == "device-access"
    assert data["token"]["refresh_token"] == "device-refresh"
    assert data["token"]["expires_at"] > 0
    assert data["auth_implementation"] == DOMAIN
    assert "password" not in data


async def test_device_flow_owns_the_same_entry_as_the_other_methods(
    hass: HomeAssistant,
) -> None:
    await _register_credential(hass)

    result = await _run_device_flow(hass)

    assert result["result"].unique_id == stable_login_identity(
        IDENTITY_SECRET,
        OEJP_AUTH_ISSUER,
        SUBJECT,
    )


async def test_device_flow_needs_a_client_id_like_the_browser_flow(
    hass: HomeAssistant,
) -> None:
    await async_setup_component(hass, "application_credentials", {})

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await _choose_method(hass, result, "device")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "missing_credentials"


@pytest.mark.parametrize(
    ("wait", "reason"),
    [
        (DeviceAuthorizationDeniedError("denied"), "user_rejected_authorize"),
        (DeviceAuthorizationExpiredError("expired"), "device_code_expired"),
        (DeviceAuthorizationTransientError("offline"), "cannot_connect"),
    ],
)
async def test_device_flow_explains_each_way_authorization_can_fail(
    hass: HomeAssistant,
    wait: Exception,
    reason: str,
) -> None:
    await _register_credential(hass)

    result = await _run_device_flow(hass, wait=wait)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


@pytest.mark.parametrize(
    ("start", "reason"),
    [
        (DeviceAuthorizationTransientError("offline"), "cannot_connect"),
        (DeviceAuthorizationError("refused"), "device_grant_unavailable"),
    ],
)
async def test_device_flow_reports_a_refused_start(
    hass: HomeAssistant,
    start: Exception,
    reason: str,
) -> None:
    """OEJP may not have enabled the device grant on this application."""
    await _register_credential(hass)

    result = await _run_device_flow(hass, start=start)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_device_flow_aborts_when_no_device_endpoint_is_recorded(
    hass: HomeAssistant,
) -> None:
    """The metadata module fails closed, and so must the flow that depends on it."""
    await _register_credential(hass)

    with patch(
        "custom_components.octopus_energy_japan.application_credentials.require_oauth_metadata",
        return_value=OejpOAuthMetadata(
            issuer=OEJP_AUTH_ISSUER,
            authorize_url="https://auth.example.test/authorize",
            token_url="https://auth.example.test/token",
            scopes=("openid",),
            authorization_scheme=AuthorizationHeaderScheme.BEARER,
            device_authorization_url=None,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        result = await _choose_method(hass, result, "device")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "device_grant_unavailable"


async def test_a_device_entry_can_replace_a_password_entry_in_place(
    hass: HomeAssistant,
) -> None:
    created = await _complete_password_flow(hass)
    entry = created["result"]
    await _register_credential(hass)

    result = await _run_device_flow(
        hass,
        source=config_entries.SOURCE_REAUTH,
        entry_id=entry.entry_id,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["auth_method"] == "device"
    assert "password" not in entry.data
    assert entry.data["token"]["access_token"] == "device-access"


async def test_device_flow_asks_which_credential_when_several_exist(
    hass: HomeAssistant,
) -> None:
    """A device grant is identified by client ID alone, so the choice must be explicit.

    The browser flow gets Home Assistant's own implementation picker. This one has to
    ask for itself, and must not silently pick whichever credential sorts first.
    """
    await async_setup_component(hass, "application_credentials", {})
    for auth_domain in ("first", "second"):
        # Distinct client IDs: Home Assistant treats a repeated one as the same
        # credential and imports it only once.
        await async_import_client_credential(
            hass,
            DOMAIN,
            ClientCredential(f"public-client-{auth_domain}", ""),
            auth_domain,
        )
    implementations = await config_entry_oauth2_flow.async_get_implementations(hass, DOMAIN)
    assert len(implementations) == 2

    client = AsyncMock()
    client.async_start = AsyncMock(side_effect=AssertionError("must ask before starting"))
    with patch(
        "custom_components.octopus_energy_japan.config_flow.OejpDeviceAuthorizationClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        result = await _choose_method(hass, result, "device")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "device"
    marker = next(iter(result["data_schema"].schema))
    assert str(marker) == "auth_implementation"
    assert sorted(result["data_schema"].schema[marker].container) == ["first", "second"]


@pytest.mark.parametrize("step", ["async_step_device_authorize", "async_step_device_finish"])
async def test_device_steps_abort_when_reached_without_a_started_authorization(
    hass: HomeAssistant,
    step: str,
) -> None:
    """A resumed or replayed flow must not act on absent state."""
    handler = OctopusEnergyJapanConfigFlow()
    handler.hass = hass
    handler.flow_id = "direct"
    handler.context = {"source": config_entries.SOURCE_USER}

    result = await getattr(handler, step)()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "device_grant_unavailable"


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
        (OejpInvalidResponseError("invalid"), "oauth_identity_unavailable"),
    ],
)
async def test_device_flow_aborts_when_the_viewer_cannot_be_identified(
    hass: HomeAssistant,
    error: Exception,
    reason: str,
) -> None:
    await _register_credential(hass)
    client = AsyncMock()
    client.async_start = AsyncMock(return_value=DEVICE_AUTHORIZATION)
    client.async_wait_for_token = AsyncMock(return_value=DEVICE_TOKEN)
    with (
        patch(
            "custom_components.octopus_energy_japan.config_flow.OejpDeviceAuthorizationClient",
            return_value=client,
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
        result = await _choose_method(hass, result, "device")
        while result["type"] is FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_device_flow_shows_the_user_code_while_it_waits(hass: HomeAssistant) -> None:
    """Displaying the code and the URL is the entire point of this method.

    The other device tests resolve the token immediately, so the progress screen never
    renders in them. This one holds the poll open and inspects what the user sees.
    """
    import asyncio

    await _register_credential(hass)
    release = asyncio.Event()

    async def blocking_wait(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await release.wait()
        return DEVICE_TOKEN

    client = AsyncMock()
    client.async_start = AsyncMock(return_value=DEVICE_AUTHORIZATION)
    client.async_wait_for_token = AsyncMock(side_effect=blocking_wait)
    with (
        patch(
            "custom_components.octopus_energy_japan.config_flow.OejpDeviceAuthorizationClient",
            return_value=client,
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
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        result = await _choose_method(hass, result, "device")

        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["step_id"] == "device_authorize"
        assert result["progress_action"] == "wait_for_device"
        placeholders = result["description_placeholders"]
        assert placeholders is not None
        assert placeholders["user_code"] == "WXYZ-1234"
        # The complete URI already carries the code, so it is preferred when offered.
        assert placeholders["url"] == DEVICE_AUTHORIZATION.verification_uri_complete

        release.set()
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        while result["type"] is FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["auth_method"] == "device"


async def test_device_flow_falls_back_to_the_plain_verification_uri(
    hass: HomeAssistant,
) -> None:
    """`verification_uri_complete` is optional in RFC 8628."""
    import asyncio

    await _register_credential(hass)
    release = asyncio.Event()

    async def blocking_wait(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await release.wait()
        return DEVICE_TOKEN

    client = AsyncMock()
    client.async_start = AsyncMock(
        return_value=DeviceAuthorization(
            device_code="device-code",
            user_code="WXYZ-1234",
            verification_uri="https://auth.example.test/device",
            verification_uri_complete=None,
            expires_in=600,
            interval=5,
        )
    )
    client.async_wait_for_token = AsyncMock(side_effect=blocking_wait)
    with patch(
        "custom_components.octopus_energy_japan.config_flow.OejpDeviceAuthorizationClient",
        return_value=client,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        result = await _choose_method(hass, result, "device")
        placeholders = result["description_placeholders"]
        assert placeholders is not None
        assert placeholders["url"] == "https://auth.example.test/device"
        release.set()
        await hass.async_block_till_done()


async def test_device_flow_uses_the_credential_the_user_selected(hass: HomeAssistant) -> None:
    await async_setup_component(hass, "application_credentials", {})
    for auth_domain in ("first", "second"):
        await async_import_client_credential(
            hass,
            DOMAIN,
            ClientCredential(f"public-client-{auth_domain}", ""),
            auth_domain,
        )

    client = AsyncMock()
    client.async_start = AsyncMock(return_value=DEVICE_AUTHORIZATION)
    client.async_wait_for_token = AsyncMock(return_value=DEVICE_TOKEN)
    with (
        patch(
            "custom_components.octopus_energy_japan.config_flow.OejpDeviceAuthorizationClient",
            return_value=client,
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
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        result = await _choose_method(hass, result, "device")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"auth_implementation": "second"},
        )
        while result["type"] is FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["auth_implementation"] == "second"
    # The selected credential's client ID is the one sent to the provider.
    assert client.async_start.await_args is not None
    assert client.async_start.await_args.args[0] == "public-client-second"
