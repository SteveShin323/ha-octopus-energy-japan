"""Provider-published OAuth metadata for OEJP."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


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


# Endpoints, issuer, and scopes are transcribed from the provider's own published
# OpenID Connect discovery document, read on 2026-08-04:
#
#     https://auth.oejp-kraken.energy/.well-known/openid-configuration
#
# They are not assumptions and not borrowed from another Kraken territory. The
# issuer really is the token URL in that document; it is recorded verbatim.
#
# The Authorization header scheme was confirmed empirically against
# `https://api.oejp-kraken.energy/v1/graphql/` on the same day: `Bearer <token>`,
# `JWT <token>`, and a bare token were all accepted, and only a missing header was
# rejected, with `KT-CT-1112`. `Bearer` is chosen because it is what OAuth 2.0
# requires and the provider accepts it.
#
# Scopes are the least-privilege read-only set for this integration. Every entry
# appears in the discovery document's `scopes_supported`. `full-customer-access` is
# deliberately excluded.
#
# The device-authorization endpoint is NOT in the discovery document, but the
# provider's own auth-server documentation lists `/device-authorization/` as one of
# its four grant types, and the live endpoint answers a POST with
# `invalid_request: Invalid client_id parameter value` rather than 404, confirmed on
# 2026-08-04. Absence from the discovery document is therefore a metadata gap, not
# an absent endpoint, and recording `None` here made the implemented RFC 8628
# client unconstructible.
#
# Neither source establishes two things this client depends on: the discovery document
# advertises no `none` token-endpoint auth method and no
# `code_challenge_methods_supported`, while the provider's documentation offers a public
# client and documents authorization with PKCE. Both read as metadata gaps; see
# `docs/adr/0001-oauth-public-client.md`.
OEJP_AUTH_ISSUER: Final = "https://auth.oejp-kraken.energy/token/"

READ_ONLY_SCOPES: Final = (
    "openid",
    "view:account-number",
    "view:account-type",
    "query:user-details",
    "query:property",
    "query:property-meters",
    "query:electricity-meter-point-details",
    "query:devices",
    "view:detailed-usage",
    "request:consumption-data",
    "query:agreements",
    "query:contracts",
    "query:billing-information",
    "query:account-payments",
)

PRODUCTION_OAUTH_METADATA: OejpOAuthMetadata | None = OejpOAuthMetadata(
    issuer=OEJP_AUTH_ISSUER,
    authorize_url="https://auth.oejp-kraken.energy/authorize/",
    token_url="https://auth.oejp-kraken.energy/token/",
    scopes=READ_ONLY_SCOPES,
    authorization_scheme=AuthorizationHeaderScheme.BEARER,
    device_authorization_url="https://auth.oejp-kraken.energy/device-authorization/",
    revocation_url="https://auth.oejp-kraken.energy/revoke-token/",
)


def require_oauth_metadata() -> OejpOAuthMetadata:
    """Return confirmed OAuth metadata or fail closed."""
    if PRODUCTION_OAUTH_METADATA is None:
        raise OAuthMetadataUnavailableError("OEJP OAuth metadata is awaiting provider confirmation")
    return PRODUCTION_OAUTH_METADATA
