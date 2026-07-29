"""Tests for OEJP GraphQL error classification."""

from custom_components.octopus_energy_japan.api.errors import (
    OejpAuthenticationError,
    OejpAuthorizationError,
    OejpRateLimitError,
    classify_graphql_errors,
)


def test_rate_limit_error_is_classified_by_code() -> None:
    error = classify_graphql_errors(
        [
            {
                "message": "Query is too complex",
                "extensions": {"errorCode": "KT-CT-1188", "errorType": "VALIDATION"},
            }
        ]
    )
    assert isinstance(error, OejpRateLimitError)
    assert error.details[0].error_code == "KT-CT-1188"


def test_authorization_error_is_classified_by_code() -> None:
    error = classify_graphql_errors(
        [
            {
                "message": "Unauthorized",
                "extensions": {"errorCode": "KT-CT-4177", "errorType": "PERMISSION"},
            }
        ]
    )
    assert isinstance(error, OejpAuthorizationError)


def test_authentication_error_is_classified_by_type() -> None:
    error = classify_graphql_errors(
        [{"message": "Token expired", "extensions": {"errorType": "AUTHENTICATION"}}]
    )
    assert isinstance(error, OejpAuthenticationError)
