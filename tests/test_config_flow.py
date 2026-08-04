"""Tests for the OEJP OAuth config flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.api import (
    CapabilitySnapshot,
    GraphQLErrorDetail,
    OejpAccount,
    OejpAuthenticationError,
    OejpInvalidResponseError,
    OejpProperty,
    OejpRateLimitError,
    OejpSupplyPoint,
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
