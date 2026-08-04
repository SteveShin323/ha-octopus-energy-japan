"""Tests for the provider's email and password login.

The failure modes matter more than the happy path here. A rejected credential and
an expired access token both reach this code as `OejpAuthenticationError`, because
OEJP reports a wrong password as `VALIDATION/KT-CT-1138`. Confusing them would turn
a changed password into an endless refresh loop against a live endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.api import (
    GraphQLErrorDetail,
    OejpAuthenticationError,
    OejpToken,
    OejpTransportError,
)
from custom_components.octopus_energy_japan.const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.octopus_energy_japan.password_auth import (
    OejpPasswordAuthSession,
    OejpPasswordCredentialRejected,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
EMAIL = "person@example.test"
PASSWORD = "correct horse"

REJECTED = OejpAuthenticationError(
    (
        GraphQLErrorDetail(
            message="GraphQL operation failed",
            error_type="VALIDATION",
            error_code="KT-CT-1138",
        ),
    )
)


def _token(access: str = "access-1", refresh: str | None = "refresh-1") -> OejpToken:
    return OejpToken(
        access_token=access,
        refresh_token=refresh,
        refresh_expires_at=NOW + timedelta(days=7),
    )


def _session(
    hass: HomeAssistant,
    data: dict[str, Any] | None = None,
) -> tuple[OejpPasswordAuthSession, MockConfigEntry]:
    entry = MockConfigEntry(domain=DOMAIN, data=data or {})
    entry.add_to_hass(hass)
    session = OejpPasswordAuthSession(
        hass,
        entry,
        AsyncMock(),
        email=EMAIL,
        password=PASSWORD,
        scheme="Bearer",
        now=lambda: NOW,
    )
    return session, entry


async def test_first_request_signs_in_and_stores_the_tokens(hass: HomeAssistant) -> None:
    session, entry = _session(hass)
    with patch(
        "custom_components.octopus_energy_japan.password_auth.async_obtain_token",
        AsyncMock(return_value=_token()),
    ) as obtain:
        header = await session.async_get_authorization_header()

    assert header == "Bearer access-1"
    obtain.assert_awaited_once()
    assert entry.data[CONF_ACCESS_TOKEN] == "access-1"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-1"
    assert entry.data[CONF_REFRESH_EXPIRES_AT] == (NOW + timedelta(days=7)).isoformat()


async def test_a_stored_access_token_is_reused_without_signing_in(hass: HomeAssistant) -> None:
    session, _ = _session(hass, {CONF_ACCESS_TOKEN: "stored"})
    with patch(
        "custom_components.octopus_energy_japan.password_auth.async_obtain_token",
        AsyncMock(),
    ) as obtain:
        assert await session.async_get_authorization_header() == "Bearer stored"

    obtain.assert_not_awaited()


async def test_refresh_renews_from_the_refresh_token_without_the_password(
    hass: HomeAssistant,
) -> None:
    session, entry = _session(
        hass,
        {
            CONF_ACCESS_TOKEN: "old",
            CONF_REFRESH_TOKEN: "refresh-1",
            CONF_REFRESH_EXPIRES_AT: (NOW + timedelta(days=3)).isoformat(),
        },
    )
    with (
        patch(
            "custom_components.octopus_energy_japan.password_auth.async_renew_token",
            AsyncMock(return_value=_token(access="access-2")),
        ) as renew,
        patch(
            "custom_components.octopus_energy_japan.password_auth.async_obtain_token",
            AsyncMock(),
        ) as obtain,
    ):
        await session.async_refresh()

    renew.assert_awaited_once()
    obtain.assert_not_awaited()
    assert entry.data[CONF_ACCESS_TOKEN] == "access-2"


async def test_refresh_signs_in_again_when_the_refresh_token_is_spent(
    hass: HomeAssistant,
) -> None:
    """A refresh token can be revoked or replaced before its seven days elapse."""
    session, entry = _session(
        hass,
        {
            CONF_ACCESS_TOKEN: "old",
            CONF_REFRESH_TOKEN: "refresh-1",
            CONF_REFRESH_EXPIRES_AT: (NOW + timedelta(days=3)).isoformat(),
        },
    )
    with (
        patch(
            "custom_components.octopus_energy_japan.password_auth.async_renew_token",
            AsyncMock(side_effect=REJECTED),
        ),
        patch(
            "custom_components.octopus_energy_japan.password_auth.async_obtain_token",
            AsyncMock(return_value=_token(access="access-3")),
        ) as obtain,
    ):
        await session.async_refresh()

    obtain.assert_awaited_once()
    assert entry.data[CONF_ACCESS_TOKEN] == "access-3"


async def test_an_expired_refresh_token_is_not_even_attempted(hass: HomeAssistant) -> None:
    """The provider does not extend the expiry on renewal, so it is known in advance."""
    session, _ = _session(
        hass,
        {
            CONF_ACCESS_TOKEN: "old",
            CONF_REFRESH_TOKEN: "refresh-1",
            CONF_REFRESH_EXPIRES_AT: (NOW - timedelta(seconds=1)).isoformat(),
        },
    )
    with (
        patch(
            "custom_components.octopus_energy_japan.password_auth.async_renew_token",
            AsyncMock(),
        ) as renew,
        patch(
            "custom_components.octopus_energy_japan.password_auth.async_obtain_token",
            AsyncMock(return_value=_token()),
        ) as obtain,
    ):
        await session.async_refresh()

    renew.assert_not_awaited()
    obtain.assert_awaited_once()


@pytest.mark.parametrize(
    "expiry",
    [None, "not a datetime", "2026-08-11T00:00:00"],
    ids=["missing", "unparseable", "naive"],
)
async def test_an_unusable_expiry_still_attempts_the_refresh_token(
    hass: HomeAssistant,
    expiry: str | None,
) -> None:
    """Let the provider decide rather than guessing the token is dead."""
    session, _ = _session(
        hass,
        {
            CONF_ACCESS_TOKEN: "old",
            CONF_REFRESH_TOKEN: "refresh-1",
            CONF_REFRESH_EXPIRES_AT: expiry,
        },
    )
    with patch(
        "custom_components.octopus_energy_japan.password_auth.async_renew_token",
        AsyncMock(return_value=_token(access="access-4")),
    ) as renew:
        await session.async_refresh()

    renew.assert_awaited_once()


async def test_a_rejected_credential_is_terminal_and_never_retried(
    hass: HomeAssistant,
) -> None:
    """The password changed, or OEJP withdrew password login.

    Both need the user, so this must surface as its own error rather than as the
    authentication error the retry wrapper would try to recover from.
    """
    session, _ = _session(hass)
    with (
        patch(
            "custom_components.octopus_energy_japan.password_auth.async_obtain_token",
            AsyncMock(side_effect=REJECTED),
        ) as obtain,
        pytest.raises(OejpPasswordCredentialRejected),
    ):
        await session.async_get_authorization_header()

    assert obtain.await_count == 1


async def test_a_transport_failure_stays_a_transport_failure(hass: HomeAssistant) -> None:
    """A network problem is retryable and must not be mistaken for a bad password."""
    session, _ = _session(hass)
    with (
        patch(
            "custom_components.octopus_energy_japan.password_auth.async_obtain_token",
            AsyncMock(side_effect=OejpTransportError("network failed")),
        ),
        pytest.raises(OejpTransportError),
    ):
        await session.async_get_authorization_header()


async def test_a_concurrent_refresh_is_not_repeated(hass: HomeAssistant) -> None:
    """Two requests rejected on the same token must renew once between them."""
    session, entry = _session(
        hass,
        {
            CONF_ACCESS_TOKEN: "old",
            CONF_REFRESH_TOKEN: "refresh-1",
            CONF_REFRESH_EXPIRES_AT: (NOW + timedelta(days=3)).isoformat(),
        },
    )
    with patch(
        "custom_components.octopus_energy_japan.password_auth.async_renew_token",
        AsyncMock(return_value=_token(access="access-5")),
    ) as renew:
        await session.async_get_authorization_header()
        await session.async_refresh()
        # The second caller's rejected token is now stale, so it must not renew again.
        await session.async_refresh()

    assert renew.await_count == 1
    assert entry.data[CONF_ACCESS_TOKEN] == "access-5"


async def test_revoke_makes_no_request_because_the_provider_forbids_it(
    hass: HomeAssistant,
) -> None:
    """`invalidateRefreshToken` is not available to an account user.

    Called as the signed-in user it returned `AUTHORIZATION/KT-CT-1111` on 2026-08-04,
    and the provider documents `KT-CT-1111` and `KT-CT-1130` Unauthorized for that
    mutation. Attempting it on every removal would be a request that can never
    succeed, so revocation is a documented no-op and the token expires on its own.
    """
    client = AsyncMock()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ACCESS_TOKEN: "access-1",
            CONF_REFRESH_TOKEN: "refresh-1",
            CONF_REFRESH_EXPIRES_AT: (NOW + timedelta(days=3)).isoformat(),
        },
    )
    entry.add_to_hass(hass)
    session = OejpPasswordAuthSession(
        hass,
        entry,
        client,
        email=EMAIL,
        password=PASSWORD,
        scheme="Bearer",
        now=lambda: NOW,
    )

    await session.async_revoke()

    client.execute.assert_not_awaited()
    # The stored token is left untouched; Home Assistant deletes the entry data.
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-1"


async def test_a_token_response_without_a_refresh_token_is_stored_as_such(
    hass: HomeAssistant,
) -> None:
    session, entry = _session(hass)
    with patch(
        "custom_components.octopus_energy_japan.password_auth.async_obtain_token",
        AsyncMock(return_value=OejpToken(access_token="only-access")),
    ):
        assert await session.async_get_authorization_header() == "Bearer only-access"

    assert entry.data[CONF_REFRESH_TOKEN] is None
    assert entry.data[CONF_REFRESH_EXPIRES_AT] is None


async def test_an_empty_scheme_sends_the_bare_token(hass: HomeAssistant) -> None:
    """The provider's own documentation shows a bare token, and the API accepts it."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ACCESS_TOKEN: "bare"})
    entry.add_to_hass(hass)
    session = OejpPasswordAuthSession(
        hass,
        entry,
        AsyncMock(),
        email=EMAIL,
        password=PASSWORD,
        scheme="",
        now=lambda: NOW,
    )

    assert await session.async_get_authorization_header() == "bare"


async def test_two_concurrent_first_requests_sign_in_once(hass: HomeAssistant) -> None:
    """The second caller must use the token the first one stored, not sign in again."""
    import asyncio

    session, entry = _session(hass)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_sign_in(*_args: Any, **_kwargs: Any) -> OejpToken:
        started.set()
        await release.wait()
        return _token(access="shared")

    with patch(
        "custom_components.octopus_energy_japan.password_auth.async_obtain_token",
        AsyncMock(side_effect=slow_sign_in),
    ) as obtain:
        first = asyncio.create_task(session.async_get_authorization_header())
        await started.wait()
        second = asyncio.create_task(session.async_get_authorization_header())
        await asyncio.sleep(0)
        release.set()
        headers = await asyncio.gather(first, second)

    assert headers == ["Bearer shared", "Bearer shared"]
    assert obtain.await_count == 1
    assert entry.data[CONF_ACCESS_TOKEN] == "shared"


async def test_an_empty_stored_refresh_token_is_treated_as_absent(hass: HomeAssistant) -> None:
    """A blank value must not reach the provider as if it were a token."""
    session, _ = _session(hass, {CONF_ACCESS_TOKEN: "old", CONF_REFRESH_TOKEN: ""})
    with (
        patch(
            "custom_components.octopus_energy_japan.password_auth.async_renew_token",
            AsyncMock(),
        ) as renew,
        patch(
            "custom_components.octopus_energy_japan.password_auth.async_obtain_token",
            AsyncMock(return_value=_token()),
        ) as obtain,
    ):
        await session.async_refresh()

    renew.assert_not_awaited()
    obtain.assert_awaited_once()
