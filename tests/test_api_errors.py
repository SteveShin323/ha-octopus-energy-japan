"""Tests for OEJP GraphQL error classification."""

from custom_components.octopus_energy_japan.api.errors import (
    GraphQLErrorDetail,
    OejpAuthenticationError,
    OejpAuthorizationError,
    OejpGraphQLError,
    OejpNotFoundError,
    OejpQueryValidationError,
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


def test_expired_kraken_token_is_classified_as_authentication() -> None:
    error = classify_graphql_errors(
        [
            {
                "message": "Token expired",
                "extensions": {
                    "errorType": "APPLICATION",
                    "errorCode": "KT-CT-1120",
                },
            }
        ]
    )
    assert isinstance(error, OejpAuthenticationError)


def test_rejected_credential_is_classified_as_authentication_despite_validation_type() -> None:
    """OEJP reports a wrong password as VALIDATION, not AUTHENTICATION."""
    error = classify_graphql_errors(
        [
            {
                "message": "Please make sure the credentials are correct",
                "extensions": {
                    "errorType": "VALIDATION",
                    "errorCode": "KT-CT-1138",
                },
            }
        ]
    )
    assert isinstance(error, OejpAuthenticationError)
    assert not isinstance(error, OejpQueryValidationError)


def test_authorization_is_not_misclassified_as_authentication() -> None:
    error = classify_graphql_errors(
        [{"message": "Not allowed", "extensions": {"errorType": "AUTHORIZATION"}}]
    )
    assert isinstance(error, OejpAuthorizationError)


def test_authorization_header_error_is_classified_by_code() -> None:
    error = classify_graphql_errors(
        [{"message": "Missing header", "extensions": {"errorCode": "KT-CT-1112"}}]
    )
    assert isinstance(error, OejpAuthorizationError)


def test_validation_and_not_found_types_are_classified() -> None:
    validation = classify_graphql_errors(
        [{"message": "Invalid field", "extensions": {"errorType": "VALIDATION"}}]
    )
    not_found = classify_graphql_errors(
        [{"message": "Missing", "extensions": {"errorType": "NOT-FOUND"}}]
    )
    assert isinstance(validation, OejpQueryValidationError)
    assert isinstance(not_found, OejpNotFoundError)


def test_error_details_and_rendered_exception_do_not_retain_provider_text() -> None:
    sensitive_email = "customer@example.com"
    sensitive_account = "A-SECRET123"
    error = classify_graphql_errors(
        [
            {
                "message": f"Account {sensitive_account} belongs to {sensitive_email}",
                "extensions": {
                    "errorType": "AUTHORIZATION",
                    "errorDescription": f"Token for {sensitive_account} was rejected",
                },
            }
        ]
    )

    rendered = f"{error!r} {error} {error.details!r}"
    assert sensitive_email not in rendered
    assert sensitive_account not in rendered
    assert error.details[0].message == "GraphQL operation failed"
    assert error.details[0].description is None


def test_error_details_drop_unsafe_structured_metadata() -> None:
    """Provider-controlled extension values must not become diagnostic output."""
    detail = GraphQLErrorDetail.from_payload(
        {
            "message": "email@example.com",
            "path": ["viewer", "account-number: A-123"],
            "extensions": {
                "errorType": "AUTHORIZATION email@example.com",
                "errorCode": "token.secret.value",
            },
        }
    )

    assert detail.error_type is None
    assert detail.error_code is None
    assert detail.path == ("viewer",)
    assert "example.com" not in str(OejpGraphQLError((detail,)))


def test_error_without_extensions_is_generic_and_sanitized() -> None:
    error = classify_graphql_errors(
        [
            {
                "message": "Account A-SECRET failed",
                "extensions": "malformed",
            }
        ]
    )

    assert type(error) is OejpGraphQLError
    assert error.details[0].error_type is None
    assert "A-SECRET" not in str(error)
