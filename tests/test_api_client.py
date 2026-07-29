"""Contract tests for the OEJP GraphQL transport."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Self, cast

import pytest
from aiohttp import ClientError, ClientSession
from custom_components.octopus_energy_japan.api import (
    OejpAuthorizationError,
    OejpGraphQLClient,
    OejpInvalidResponseError,
    OejpTimeoutError,
    OejpTransportError,
)


class FakeResponse:
    """Minimal aiohttp response context used by transport contract tests."""

    def __init__(
        self,
        payload: object = None,
        *,
        status: int = 200,
        json_error: Exception | None = None,
        enter_delay: float = 0,
    ) -> None:
        self.status = status
        self._payload = payload
        self._json_error = json_error
        self._enter_delay = enter_delay
        self.read_called = False

    async def __aenter__(self) -> Self:
        if self._enter_delay:
            await asyncio.sleep(self._enter_delay)
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, *, content_type: None = None) -> object:
        assert content_type is None
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    async def read(self) -> bytes:
        self.read_called = True
        return b""


class FakeSession:
    """Capture one post request and return a configured response."""

    def __init__(
        self,
        response: FakeResponse | None = None,
        *,
        post_error: ClientError | None = None,
    ) -> None:
        self.response = response
        self.post_error = post_error
        self.request: dict[str, Any] | None = None

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> FakeResponse:
        self.request = {"url": url, "json": dict(json), "headers": dict(headers)}
        if self.post_error is not None:
            raise self.post_error
        assert self.response is not None
        return self.response


def _client(
    session: FakeSession,
    *,
    timeout_seconds: float = 30,
) -> OejpGraphQLClient:
    return OejpGraphQLClient(
        cast("ClientSession", session),
        endpoint="https://example.test/graphql",
        timeout_seconds=timeout_seconds,
    )


async def test_execute_returns_data_and_builds_authenticated_request() -> None:
    session = FakeSession(FakeResponse({"data": {"viewer": {"id": "viewer"}}}))

    data = await _client(session).execute(
        "query Viewer($active: Boolean!) { viewer { id } }",
        {"active": True},
        access_token="access-token",
    )

    assert data == {"viewer": {"id": "viewer"}}
    assert session.request == {
        "url": "https://example.test/graphql",
        "json": {
            "query": "query Viewer($active: Boolean!) { viewer { id } }",
            "variables": {"active": True},
        },
        "headers": {
            "Accept": "application/json",
            "Authorization": "JWT access-token",
        },
    }


async def test_execute_optional_preserves_partial_data_and_sanitized_errors() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "data": {"viewer": {"accounts": []}},
                "errors": [
                    {
                        "message": "Account A-SECRET cannot access bills",
                        "path": ["viewer", "accounts", 0, "bills"],
                        "extensions": {
                            "errorType": "AUTHORIZATION",
                            "errorCode": "KT-CT-4177",
                        },
                    }
                ],
            }
        )
    )

    result = await _client(session).execute_optional("query { viewer { accounts { bills } } }")

    assert result.data == {"viewer": {"accounts": []}}
    assert len(result.errors) == 1
    assert result.errors[0].path == ("viewer", "accounts", 0, "bills")
    assert "A-SECRET" not in repr(result.errors)


async def test_execute_strict_rejects_partial_errors() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "data": {"viewer": None},
                "errors": [
                    {
                        "message": "Not allowed",
                        "extensions": {"errorType": "AUTHORIZATION"},
                    }
                ],
            }
        )
    )

    with pytest.raises(OejpAuthorizationError):
        await _client(session).execute("query { viewer { id } }")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"data": "not-an-object"},
        {"errors": "not-a-list"},
        {"errors": ["not-an-object"]},
        {},
    ],
)
async def test_execute_optional_rejects_malformed_payloads(payload: object) -> None:
    session = FakeSession(FakeResponse(payload))

    with pytest.raises(OejpInvalidResponseError):
        await _client(session).execute_optional("query { viewer { id } }")


async def test_execute_optional_allows_errors_without_data() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "errors": [
                    {
                        "message": "Not allowed",
                        "extensions": {"errorType": "AUTHORIZATION"},
                    }
                ]
            }
        )
    )

    result = await _client(session).execute_optional("query { viewer { id } }")

    assert result.data is None
    assert len(result.errors) == 1


async def test_invalid_json_is_wrapped() -> None:
    session = FakeSession(FakeResponse(json_error=ValueError("private response body")))

    with pytest.raises(OejpInvalidResponseError, match="not valid JSON"):
        await _client(session).execute("query { viewer { id } }")


async def test_http_error_is_wrapped_and_body_is_consumed() -> None:
    response = FakeResponse(status=503)
    session = FakeSession(response)

    with pytest.raises(OejpTransportError, match="HTTP 503"):
        await _client(session).execute("query { viewer { id } }")

    assert response.read_called


async def test_client_error_is_wrapped() -> None:
    session = FakeSession(post_error=ClientError("network secret"))

    with pytest.raises(OejpTransportError, match="network request failed"):
        await _client(session).execute("query { viewer { id } }")


async def test_timeout_is_wrapped() -> None:
    session = FakeSession(FakeResponse(enter_delay=0.05))

    with pytest.raises(OejpTimeoutError):
        await _client(session, timeout_seconds=0.001).execute("query { viewer { id } }")
