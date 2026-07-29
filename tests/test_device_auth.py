"""Tests for RFC 8628 Device Authorization Grant."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self, cast

import pytest
from aiohttp import ClientError, ClientSession
from custom_components.octopus_energy_japan.api import (
    DeviceAuthorization,
    DeviceAuthorizationDeniedError,
    DeviceAuthorizationError,
    DeviceAuthorizationExpiredError,
    DeviceAuthorizationPendingError,
    DeviceAuthorizationSlowDownError,
    DeviceAuthorizationTransientError,
    OejpDeviceAuthorizationClient,
)

DEVICE_URL = "https://auth.example.test/device"
TOKEN_URL = "https://auth.example.test/token"


class FakeResponse:
    """Minimal response context for OAuth form requests."""

    def __init__(
        self,
        payload: object,
        *,
        status: int = 200,
        json_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.payload = payload
        self.json_error = json_error
        self.read_called = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, *, content_type: None = None) -> object:
        assert content_type is None
        if self.json_error:
            raise self.json_error
        return self.payload

    async def read(self) -> bytes:
        self.read_called = True
        return b""


class FakeSession:
    """Return queued responses and capture form data."""

    def __init__(
        self,
        *responses: FakeResponse,
        error: ClientError | None = None,
    ) -> None:
        self.responses = list(responses)
        self.error = error
        self.requests: list[tuple[str, dict[str, str]]] = []

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, str],
    ) -> FakeResponse:
        self.requests.append((url, dict(data)))
        if self.error:
            raise self.error
        return self.responses.pop(0)


def _client(
    session: FakeSession,
    **kwargs: Any,
) -> OejpDeviceAuthorizationClient:
    return OejpDeviceAuthorizationClient(
        cast("ClientSession", session),
        device_authorization_url=DEVICE_URL,
        token_url=TOKEN_URL,
        **kwargs,
    )


async def test_start_device_authorization_parses_instructions() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "device_code": "device-code",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://auth.example.test/activate",
                "verification_uri_complete": "https://auth.example.test/activate?code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 7,
            }
        )
    )

    authorization = await _client(session).async_start(
        "public-client",
        ("openid", "account:read"),
    )

    assert authorization.device_code == "device-code"
    assert authorization.interval == 7
    assert session.requests == [
        (
            DEVICE_URL,
            {
                "client_id": "public-client",
                "scope": "openid account:read",
            },
        )
    ]


async def test_start_device_authorization_defaults_poll_interval() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "device_code": "device-code",
                "user_code": "ABCD",
                "verification_uri": "https://auth.example.test/activate",
                "expires_in": 600,
            }
        )
    )

    authorization = await _client(session).async_start("public-client", ())

    assert authorization.interval == 5
    assert authorization.verification_uri_complete is None


async def test_poll_token_returns_only_supported_oauth_fields() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "openid",
                "provider_private": "discard",
            }
        )
    )

    token = await _client(session).async_poll_token("public-client", "device-code")

    assert token == {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "openid",
    }
    assert session.requests[0][1]["grant_type"] == ("urn:ietf:params:oauth:grant-type:device_code")
    assert "client_secret" not in session.requests[0][1]


@pytest.mark.parametrize(
    ("oauth_error", "error_class"),
    [
        ("authorization_pending", DeviceAuthorizationPendingError),
        ("slow_down", DeviceAuthorizationSlowDownError),
        ("access_denied", DeviceAuthorizationDeniedError),
        ("expired_token", DeviceAuthorizationExpiredError),
        ("invalid_grant", DeviceAuthorizationError),
    ],
)
async def test_poll_token_classifies_safe_oauth_errors(
    oauth_error: str,
    error_class: type[DeviceAuthorizationError],
) -> None:
    session = FakeSession(FakeResponse({"error": oauth_error}, status=400))

    with pytest.raises(error_class):
        await _client(session).async_poll_token("public-client", "device-code")


async def test_wait_for_token_honors_pending_and_slow_down() -> None:
    session = FakeSession(
        FakeResponse({"error": "authorization_pending"}, status=400),
        FakeResponse({"error": "slow_down"}, status=400),
        FakeResponse({"access_token": "access", "expires_in": 3600}),
    )
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    token = await _client(
        session,
        sleep=record_sleep,
        monotonic=lambda: 0,
    ).async_wait_for_token(
        "public-client",
        DeviceAuthorization(
            device_code="device-code",
            user_code="ABCD",
            verification_uri="https://auth.example.test/activate",
            verification_uri_complete=None,
            expires_in=600,
            interval=5,
        ),
    )

    assert token["access_token"] == "access"
    assert sleeps == [5, 5, 10]


async def test_wait_for_token_stops_at_deadline() -> None:
    clock = iter((0.0, 0.0, 2.0))
    session = FakeSession()

    async def no_sleep(_seconds: float) -> None:
        return None

    with pytest.raises(DeviceAuthorizationExpiredError):
        await _client(
            session,
            sleep=no_sleep,
            monotonic=lambda: next(clock),
        ).async_wait_for_token(
            "public-client",
            DeviceAuthorization(
                device_code="device-code",
                user_code="ABCD",
                verification_uri="https://auth.example.test/activate",
                verification_uri_complete=None,
                expires_in=1,
                interval=5,
            ),
        )


async def test_wait_for_token_does_not_poll_after_deadline() -> None:
    clock = iter((0.0, 2.0))
    session = FakeSession()

    with pytest.raises(DeviceAuthorizationExpiredError):
        await _client(
            session,
            monotonic=lambda: next(clock),
        ).async_wait_for_token(
            "public-client",
            DeviceAuthorization(
                device_code="device-code",
                user_code="ABCD",
                verification_uri="https://auth.example.test/activate",
                verification_uri_complete=None,
                expires_in=1,
                interval=5,
            ),
        )

    assert session.requests == []


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {
            "device_code": "",
            "user_code": "ABCD",
            "verification_uri": "https://example.test",
            "expires_in": 600,
        },
        {
            "device_code": "device",
            "user_code": "ABCD",
            "verification_uri": "https://example.test",
            "expires_in": True,
        },
        {
            "device_code": "device",
            "user_code": "ABCD",
            "verification_uri": "https://example.test",
            "expires_in": -1,
        },
    ],
)
async def test_device_authorization_rejects_malformed_response(payload: object) -> None:
    session = FakeSession(FakeResponse(payload))

    with pytest.raises(DeviceAuthorizationError):
        await _client(session).async_start("public-client", ())


async def test_device_authorization_wraps_network_and_server_errors() -> None:
    with pytest.raises(DeviceAuthorizationTransientError):
        await _client(FakeSession(error=ClientError("secret network detail"))).async_start(
            "public-client", ()
        )

    response = FakeResponse(None, status=503, json_error=ValueError("not json"))
    with pytest.raises(DeviceAuthorizationTransientError):
        await _client(FakeSession(response)).async_start(
            "public-client",
            (),
        )
    assert response.read_called


async def test_start_device_authorization_rejects_client_error_response() -> None:
    with pytest.raises(DeviceAuthorizationError, match="rejected"):
        await _client(
            FakeSession(FakeResponse({"error": "invalid_client"}, status=400))
        ).async_start("public-client", ())


async def test_device_authorization_rejects_invalid_json_safely() -> None:
    session = FakeSession(FakeResponse(None, json_error=ValueError("private response body")))

    with pytest.raises(DeviceAuthorizationError, match="not valid JSON"):
        await _client(session).async_start("public-client", ())
