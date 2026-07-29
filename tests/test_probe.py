"""Tests for synthetic OEJP contract fixtures."""

from __future__ import annotations

import hashlib

import pytest
from custom_components.octopus_energy_japan.probe import (
    SyntheticFixtureSanitizer,
    UnsafeFixtureError,
    assert_contract_provenance,
    assert_safe_fixture,
    build_contract_fixture,
)

QUERY = "query ViewerAccounts { viewer { accounts { number } } }"


def test_contract_fixture_is_deterministic_and_removes_customer_data() -> None:
    response = {
        "viewer": {
            "id": "viewer-private",
            "email": "customer@example.jp",
            "accounts": [
                {
                    "number": "A-12345678",
                    "billingName": "Example Customer",
                    "properties": [
                        {
                            "address": "1-2-3 Private Street",
                            "electricitySupplyPoints": [
                                {
                                    "spin": "SPIN-123456",
                                    "meters": [{"serialNumber": "METER-987"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    }

    first = build_contract_fixture("viewer_accounts", QUERY, response)
    second = build_contract_fixture("viewer_accounts", QUERY, response)

    assert first == second
    rendered = repr(first)
    for private_value in (
        "viewer-private",
        "customer@example.jp",
        "A-12345678",
        "Example Customer",
        "1-2-3 Private Street",
        "SPIN-123456",
        "METER-987",
    ):
        assert private_value not in rendered
    assert first["_meta"]["query_sha256"] == hashlib.sha256(QUERY.encode()).hexdigest()
    assert first["_meta"]["synthetic"] is True
    assert first["_meta"]["source"] == "synthetic-test-data"


def test_equal_values_receive_stable_placeholders_within_document() -> None:
    sanitizer = SyntheticFixtureSanitizer()
    sanitized = sanitizer.sanitize(
        {"accounts": [{"number": "A-1"}, {"number": "A-1"}, {"number": "A-2"}]}
    )

    accounts = sanitized["accounts"]
    assert accounts[0]["number"] == accounts[1]["number"]
    assert accounts[0]["number"] != accounts[2]["number"]


def test_sanitizer_handles_sensitive_lists_tuples_empty_values_and_scalars() -> None:
    sanitizer = SyntheticFixtureSanitizer()

    sanitized = sanitizer.sanitize(
        {
            "email": ["first@example.jp", "second@example.jp"],
            "name": "",
            "address": {"line1": "Private street", "town": "Private town"},
            "futureTuple": ("plain", 7),
            "enabled": True,
            "missing": None,
        }
    )

    assert sanitized["email"] == [
        "<synthetic:email:1>",
        "<synthetic:email:2>",
    ]
    assert sanitized["name"] == "<synthetic:name:empty>"
    assert sanitized["address"] == {
        "line1": "<synthetic:address:1>",
        "town": "<synthetic:address:2>",
    }
    assert sanitized["futureTuple"] == ["plain", 7]
    assert sanitized["enabled"] is True
    assert sanitized["missing"] is None


@pytest.mark.parametrize(
    "unsafe",
    [
        {"email": "customer@example.jp"},
        {"token": "Bearer definitely-a-real-token"},
        {"safe": "eyJhbGciOiJIUzI1NiJ9.payload.signature"},
        {"safe": "-----BEGIN PRIVATE KEY-----"},
        {"accountNumber": "A-123"},
        {"accountNumber": 123},
    ],
)
def test_scanner_rejects_credentials_and_unsanitized_pii(
    unsafe: dict[str, object],
) -> None:
    with pytest.raises(UnsafeFixtureError):
        assert_safe_fixture(unsafe)


def test_scanner_rejects_original_values_even_when_shape_is_unknown() -> None:
    with pytest.raises(UnsafeFixtureError, match="original value"):
        assert_safe_fixture(
            {"unknownFutureField": "private-provider-value"},
            forbidden_values=frozenset({"private-provider-value"}),
        )


def test_secret_like_unknown_string_is_synthetic() -> None:
    sanitizer = SyntheticFixtureSanitizer()
    value = "x" * 64

    sanitized = sanitizer.sanitize({"futureField": value})

    assert sanitized["futureField"] == "<synthetic:secret:1>"
    assert_safe_fixture(sanitized, forbidden_values=sanitizer.raw_values)


def test_sanitizer_rejects_unsupported_sensitive_value() -> None:
    with pytest.raises(UnsafeFixtureError, match="unsupported value"):
        SyntheticFixtureSanitizer().sanitize({"email": object()})


def test_future_sensitive_key_shapes_are_redacted() -> None:
    sanitizer = SyntheticFixtureSanitizer()

    sanitized = sanitizer.sanitize(
        {
            "addressLine1": "Private address",
            "fullName": "Private name",
            "propertyId": "property-private",
            "oauthTokenValue": "short-token",
            "clientSecretValue": "short-secret",
            "primaryEmailValue": "private@example.jp",
            "postalCodeValue": "100-0001",
            "supplyPointReference": "supply-private",
            "meterDeviceId": "meter-private",
            "registerChannelId": "register-private",
            "deviceChannelId": "device-private",
            "opaqueIdentifier": "identifier-private",
        }
    )

    assert all(
        isinstance(value, str) and value.startswith("<synthetic:") for value in sanitized.values()
    )


def test_schema_capability_fixture_preserves_only_graphql_field_names() -> None:
    fixture = build_contract_fixture(
        "schema_capabilities",
        'query Capabilities { __type(name: "Query") { fields { name } } }',
        {
            "queryType": {
                "fields": [
                    {"name": "supplyPoints"},
                    {"name": "viewer"},
                ]
            }
        },
    )

    assert fixture["response"] == {
        "queryType": {
            "fields": [
                {"name": "supplyPoints"},
                {"name": "viewer"},
            ]
        }
    }
    assert_safe_fixture(fixture)


def test_schema_capability_fixture_redacts_invalid_graphql_name() -> None:
    fixture = build_contract_fixture(
        "schema_capabilities",
        'query Capabilities { __type(name: "Query") { name } }',
        {"queryType": {"name": "customer@example.jp"}},
    )

    assert fixture["response"] == {"queryType": {"name": "<synthetic:name:1>"}}


def _valid_fixture() -> dict[str, object]:
    return build_contract_fixture("viewer", QUERY, {"viewer": {}})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 99),
        ("sanitizer_version", 99),
        ("synthetic", False),
        ("source", "unknown"),
        ("operation", 7),
        ("operation", "Invalid Operation"),
        ("query_sha256", 7),
        ("query_sha256", "bad"),
    ],
)
def test_contract_provenance_rejects_invalid_metadata(
    field: str,
    value: object,
) -> None:
    fixture = _valid_fixture()
    metadata = fixture["_meta"]
    assert isinstance(metadata, dict)
    metadata[field] = value

    with pytest.raises(UnsafeFixtureError):
        assert_contract_provenance(fixture)


@pytest.mark.parametrize("metadata", [None, []])
def test_contract_provenance_requires_metadata_object(metadata: object) -> None:
    fixture = _valid_fixture()
    fixture["_meta"] = metadata

    with pytest.raises(UnsafeFixtureError, match="provenance"):
        assert_contract_provenance(fixture)


def test_contract_provenance_requires_object_response() -> None:
    fixture = _valid_fixture()
    fixture["response"] = []

    with pytest.raises(UnsafeFixtureError, match="response"):
        assert_contract_provenance(fixture)
