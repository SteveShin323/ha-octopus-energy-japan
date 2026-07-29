"""Tests for fail-closed provider OAuth metadata."""

from unittest.mock import patch

import pytest
from custom_components.octopus_energy_japan.oauth_metadata import (
    AuthorizationHeaderScheme,
    OAuthMetadataUnavailableError,
    OejpOAuthMetadata,
    require_oauth_metadata,
)


def test_production_metadata_is_unavailable_until_provider_confirmation() -> None:
    with pytest.raises(OAuthMetadataUnavailableError):
        require_oauth_metadata()


def test_confirmed_production_metadata_is_returned() -> None:
    metadata = OejpOAuthMetadata(
        issuer="https://auth.example.test",
        authorize_url="https://auth.example.test/authorize",
        token_url="https://auth.example.test/token",
        scopes=("openid",),
        authorization_scheme=AuthorizationHeaderScheme.BEARER,
    )
    with patch(
        "custom_components.octopus_energy_japan.oauth_metadata."
        "PRODUCTION_OAUTH_METADATA",
        metadata,
    ):
        assert require_oauth_metadata() is metadata
