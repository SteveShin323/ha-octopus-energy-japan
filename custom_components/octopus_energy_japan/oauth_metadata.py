"""Provider-confirmed OAuth metadata for OEJP."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AuthorizationHeaderScheme(StrEnum):
    """Supported provider-confirmed Authorization header schemes."""

    BEARER = "Bearer"
    JWT = "JWT"
    RAW = ""


@dataclass(frozen=True, slots=True)
class OejpOAuthMetadata:
    """OAuth endpoints and behavior confirmed by OEJP."""

    issuer: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    authorization_scheme: AuthorizationHeaderScheme
    device_authorization_url: str | None = None
    revocation_url: str | None = None


class OAuthMetadataUnavailableError(RuntimeError):
    """Raised until OEJP confirms production OAuth metadata."""


# Do not populate this value from assumptions or another Kraken territory.
PRODUCTION_OAUTH_METADATA: OejpOAuthMetadata | None = None


def require_oauth_metadata() -> OejpOAuthMetadata:
    """Return confirmed OAuth metadata or fail closed."""
    if PRODUCTION_OAUTH_METADATA is None:
        raise OAuthMetadataUnavailableError("OEJP OAuth metadata is awaiting provider confirmation")
    return PRODUCTION_OAUTH_METADATA
