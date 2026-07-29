"""Purpose-specific OEJP GraphQL operations and response parsers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .client import OejpGraphQLClient
from .errors import OejpInvalidResponseError
from .models import OejpAccount

OBTAIN_TOKEN_MUTATION = """
mutation ObtainToken($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) {
    token
    refreshToken
    refreshExpiresIn
  }
}
"""

VIEWER_ACCOUNTS_QUERY = """
query ViewerAccounts {
  viewer {
    accounts {
      number
    }
  }
}
"""

VIEWER_IDENTITY_QUERY = """
query ViewerIdentity {
  viewer {
    id
  }
}
"""


@dataclass(frozen=True, slots=True)
class OejpToken:
    """Tokens returned by the legacy OEJP Kraken-token operation."""

    access_token: str
    refresh_token: str | None = None
    refresh_expires_at: datetime | None = None


async def async_obtain_token(
    client: OejpGraphQLClient,
    email: str,
    password: str,
) -> OejpToken:
    """Authenticate with OEJP and return a validated token response."""
    data = await client.execute(
        OBTAIN_TOKEN_MUTATION,
        {"input": {"email": email, "password": password}},
    )
    raw_token = data.get("obtainKrakenToken")
    if not isinstance(raw_token, dict):
        raise OejpInvalidResponseError("Token response was missing obtainKrakenToken")

    access_token = _required_string(raw_token, "token", "Token response")
    refresh_token = _optional_string(raw_token.get("refreshToken"))
    refresh_expires_at = _optional_unix_timestamp(raw_token.get("refreshExpiresIn"))
    return OejpToken(
        access_token=access_token,
        refresh_token=refresh_token,
        refresh_expires_at=refresh_expires_at,
    )


async def async_discover_accounts(
    client: OejpGraphQLClient,
    authorization_header: str,
) -> tuple[OejpAccount, ...]:
    """Return every account visible to the authenticated OEJP viewer."""
    data = await client.execute(
        VIEWER_ACCOUNTS_QUERY,
        authorization_header=authorization_header,
    )
    viewer = data.get("viewer")
    if not isinstance(viewer, dict):
        raise OejpInvalidResponseError("Account response was missing viewer")
    raw_accounts = viewer.get("accounts")
    if not isinstance(raw_accounts, list):
        raise OejpInvalidResponseError("Account response did not contain an accounts list")

    accounts_by_number: dict[str, OejpAccount] = {}
    for raw_account in raw_accounts:
        if not isinstance(raw_account, dict):
            raise OejpInvalidResponseError("Account response contained a malformed account")
        number = _required_string(raw_account, "number", "Account response")
        accounts_by_number[number] = OejpAccount(
            number=number,
            status=_optional_string(raw_account.get("status")),
        )
    return tuple(accounts_by_number[number] for number in sorted(accounts_by_number))


async def async_get_viewer_identity(
    client: OejpGraphQLClient,
    authorization_header: str,
) -> str:
    """Return the stable provider identifier for the authenticated viewer."""
    data = await client.execute(
        VIEWER_IDENTITY_QUERY,
        authorization_header=authorization_header,
    )
    viewer = data.get("viewer")
    if not isinstance(viewer, dict):
        raise OejpInvalidResponseError("Viewer identity response was missing viewer")
    return _required_string(viewer, "id", "Viewer identity response")


def _required_string(payload: dict[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OejpInvalidResponseError(f"{context} was missing {key}")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_unix_timestamp(value: object) -> datetime | None:
    """Parse an optional Unix timestamp from the Kraken token response."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise OejpInvalidResponseError("Token response contained invalid refreshExpiresIn")
    if isinstance(value, int):
        timestamp = value
    elif isinstance(value, str):
        try:
            timestamp = int(value)
        except ValueError as err:
            raise OejpInvalidResponseError(
                "Token response contained invalid refreshExpiresIn"
            ) from err
    else:
        raise OejpInvalidResponseError("Token response contained invalid refreshExpiresIn")

    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError) as err:
        raise OejpInvalidResponseError("Token response contained invalid refreshExpiresIn") from err
