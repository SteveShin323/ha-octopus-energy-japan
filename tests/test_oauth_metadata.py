"""Tests for provider-published OAuth metadata."""

from unittest.mock import patch

import pytest
from custom_components.octopus_energy_japan.oauth_metadata import (
    OEJP_AUTH_ISSUER,
    READ_ONLY_SCOPES,
    AuthorizationHeaderScheme,
    OAuthMetadataUnavailableError,
    OejpOAuthMetadata,
    require_oauth_metadata,
)

# Scopes the provider advertises in its discovery document, read 2026-08-04. Only
# the entries this integration could plausibly want are listed; the subset check
# below then fails if a requested scope is not advertised.
ADVERTISED_SCOPES = frozenset(
    {
        "openid",
        "full-customer-access",
        "view:account-number",
        "view:account-type",
        "view:detailed-usage",
        "view:sensitive-customer-information",
        "query:user-details",
        "query:property",
        "query:property-meters",
        "query:electricity-meter-point-details",
        "query:devices",
        "query:agreements",
        "query:contracts",
        "query:billing-information",
        "query:account-payments",
        "query:payment-instructions",
        "request:consumption-data",
        "submit:meter-readings",
    }
)


def test_published_metadata_is_available_and_uses_the_provider_endpoints() -> None:
    metadata = require_oauth_metadata()

    assert metadata.issuer == OEJP_AUTH_ISSUER
    assert metadata.authorize_url == "https://auth.oejp-kraken.energy/authorize/"
    assert metadata.token_url == "https://auth.oejp-kraken.energy/token/"
    assert metadata.revocation_url == "https://auth.oejp-kraken.energy/revoke-token/"
    for url in (metadata.issuer, metadata.authorize_url, metadata.token_url):
        assert url.startswith("https://auth.oejp-kraken.energy/")


def test_authorization_scheme_is_the_one_the_api_accepts() -> None:
    """Confirmed live: a missing header gives KT-CT-1112 and Bearer is accepted."""
    assert require_oauth_metadata().authorization_scheme is AuthorizationHeaderScheme.BEARER


def test_device_authorization_is_not_claimed_because_it_is_not_advertised() -> None:
    assert require_oauth_metadata().device_authorization_url is None


def test_requested_scopes_are_least_privilege_and_all_advertised() -> None:
    scopes = set(require_oauth_metadata().scopes)

    assert scopes == set(READ_ONLY_SCOPES)
    assert len(scopes) == len(READ_ONLY_SCOPES)
    assert "openid" in scopes
    assert scopes <= ADVERTISED_SCOPES
    assert "full-customer-access" not in scopes
    assert not any(
        scope.startswith(("update:", "create:", "delete:", "manage:", "submit:", "switch:"))
        for scope in scopes
    )


def test_metadata_still_fails_closed_when_it_is_absent() -> None:
    with (
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.PRODUCTION_OAUTH_METADATA",
            None,
        ),
        pytest.raises(OAuthMetadataUnavailableError),
    ):
        require_oauth_metadata()


def test_explicitly_supplied_metadata_is_returned_unchanged() -> None:
    metadata = OejpOAuthMetadata(
        issuer="https://auth.example.test",
        authorize_url="https://auth.example.test/authorize",
        token_url="https://auth.example.test/token",
        scopes=("openid",),
        authorization_scheme=AuthorizationHeaderScheme.BEARER,
    )
    with patch(
        "custom_components.octopus_energy_japan.oauth_metadata.PRODUCTION_OAUTH_METADATA",
        metadata,
    ):
        assert require_oauth_metadata() is metadata
