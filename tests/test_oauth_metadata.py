"""Tests for fail-closed provider OAuth metadata."""

import pytest
from custom_components.octopus_energy_japan.oauth_metadata import (
    OAuthMetadataUnavailableError,
    require_oauth_metadata,
)


def test_production_metadata_is_unavailable_until_provider_confirmation() -> None:
    with pytest.raises(OAuthMetadataUnavailableError):
        require_oauth_metadata()
