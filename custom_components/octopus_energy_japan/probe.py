"""Safe transformation of live OEJP responses into synthetic contract fixtures."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

FIXTURE_SCHEMA_VERSION: Final = 1
SANITIZER_VERSION: Final = 1

_PLACEHOLDER_PATTERN = re.compile(r"^<synthetic:[a-z_]+:[1-9][0-9]*>$")
_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_AUTH_PATTERN = re.compile(r"(?i)\b(?:bearer|jwt)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_LONG_SECRET_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{48,}\b")
_OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTRACT_SOURCES: Final = {
    "authorized-local-read-only-probe",
    "official-oejp-example",
    "synthetic-test-data",
}

_SENSITIVE_KEYS: Final[dict[str, str]] = {
    "accesstoken": "token",
    "accountnumber": "account",
    "address": "address",
    "billingname": "name",
    "clientsecret": "token",
    "deviceid": "device",
    "deviceidentifier": "device",
    "email": "email",
    "familyname": "name",
    "givenname": "name",
    "id": "identifier",
    "idtoken": "token",
    "meterid": "meter",
    "meterserialnumber": "meter",
    "mpan": "supply_point",
    "name": "name",
    "number": "account",
    "postaladdress": "address",
    "postcode": "address",
    "refreshtoken": "token",
    "registerid": "register",
    "serialnumber": "meter",
    "spin": "supply_point",
    "subject": "viewer",
    "supplypointid": "supply_point",
    "token": "token",
}


class UnsafeFixtureError(ValueError):
    """A candidate fixture still contains credential or customer data."""


@dataclass(slots=True)
class SyntheticFixtureSanitizer:
    """Replace customer values with deterministic per-document placeholders."""

    _counters: dict[str, int] = field(default_factory=dict)
    _replacements: dict[tuple[str, str], str] = field(default_factory=dict)
    _raw_values: set[str] = field(default_factory=set)

    @property
    def raw_values(self) -> frozenset[str]:
        """Return sensitive source strings for an independent leak check."""
        return frozenset(self._raw_values)

    def sanitize(self, value: Any, *, forced_category: str | None = None) -> Any:
        """Return a JSON-compatible synthetic copy without mutating input."""
        if forced_category is not None and value is not None:
            if isinstance(value, (str, int, float)):
                return self._placeholder(forced_category, str(value))
            if isinstance(value, Mapping):
                return {
                    str(key): self.sanitize(item, forced_category=forced_category)
                    for key, item in value.items()
                }
            if isinstance(value, (list, tuple)):
                return [self.sanitize(item, forced_category=forced_category) for item in value]
            raise UnsafeFixtureError(
                f"Sensitive {forced_category} field contained an unsupported value"
            )

        if isinstance(value, Mapping):
            sanitized: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = str(raw_key)
                category = _category_for_key(key)
                sanitized[key] = self.sanitize(item, forced_category=category)
            return sanitized
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [self.sanitize(item) for item in value]
        if isinstance(value, str) and _looks_secret(value):
            return self._placeholder("secret", value)
        return value

    def _placeholder(self, category: str, raw_value: str) -> str:
        if not raw_value:
            return f"<synthetic:{category}:empty>"
        replacement_key = (category, raw_value)
        if replacement := self._replacements.get(replacement_key):
            return replacement
        next_value = self._counters.get(category, 0) + 1
        self._counters[category] = next_value
        replacement = f"<synthetic:{category}:{next_value}>"
        self._replacements[replacement_key] = replacement
        self._raw_values.add(raw_value)
        return replacement


def build_contract_fixture(
    operation_name: str,
    query: str,
    response: Mapping[str, Any],
    *,
    source: str = "synthetic-test-data",
) -> dict[str, Any]:
    """Build and verify one deterministic, provenance-bearing fixture."""
    sanitizer = SyntheticFixtureSanitizer()
    sanitized = sanitizer.sanitize(response)
    fixture = {
        "_meta": {
            "schema_version": FIXTURE_SCHEMA_VERSION,
            "sanitizer_version": SANITIZER_VERSION,
            "source": source,
            "operation": operation_name,
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            "synthetic": True,
        },
        "response": sanitized,
    }
    assert_safe_fixture(fixture, forbidden_values=sanitizer.raw_values)
    assert_contract_provenance(fixture)
    return fixture


def assert_contract_provenance(fixture: Mapping[str, Any]) -> None:
    """Require verifiable synthetic fixture and query provenance."""
    metadata = fixture.get("_meta")
    if not isinstance(metadata, Mapping):
        raise UnsafeFixtureError("Contract fixture is missing _meta provenance")
    if metadata.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise UnsafeFixtureError("Contract fixture schema version is unsupported")
    if metadata.get("sanitizer_version") != SANITIZER_VERSION:
        raise UnsafeFixtureError("Contract fixture sanitizer version is unsupported")
    if metadata.get("synthetic") is not True:
        raise UnsafeFixtureError("Contract fixture is not marked synthetic")
    source = metadata.get("source")
    if source not in _CONTRACT_SOURCES:
        raise UnsafeFixtureError("Contract fixture source is unsupported")
    operation = metadata.get("operation")
    if not isinstance(operation, str) or _OPERATION_PATTERN.fullmatch(operation) is None:
        raise UnsafeFixtureError("Contract fixture operation is invalid")
    query_digest = metadata.get("query_sha256")
    if not isinstance(query_digest, str) or re.fullmatch(r"[0-9a-f]{64}", query_digest) is None:
        raise UnsafeFixtureError("Contract fixture query digest is invalid")
    if not isinstance(fixture.get("response"), Mapping):
        raise UnsafeFixtureError("Contract fixture response must be an object")


def assert_safe_fixture(
    fixture: Mapping[str, Any],
    *,
    forbidden_values: frozenset[str] = frozenset(),
) -> None:
    """Reject credentials, PII, or unsanitized values in a fixture."""
    for path, value in _walk_leaves(fixture):
        if isinstance(value, str) and value in forbidden_values:
            raise UnsafeFixtureError(f"Fixture contains an original value at {path}")

        key = _normalize_key(path.rsplit(".", maxsplit=1)[-1])
        category = _category_for_key(key)
        if (
            category is not None
            and value is not None
            and not (
                isinstance(value, str)
                and (
                    _PLACEHOLDER_PATTERN.fullmatch(value)
                    or value == f"<synthetic:{category}:empty>"
                )
            )
        ):
            raise UnsafeFixtureError(f"Fixture contains unsanitized {category} data at {path}")

        if not isinstance(value, str):
            continue
        is_query_digest = path == "$._meta.query_sha256" and bool(
            re.fullmatch(r"[0-9a-f]{64}", value)
        )
        if (not is_query_digest and _looks_secret(value)) or _EMAIL_PATTERN.search(value):
            raise UnsafeFixtureError(f"Fixture contains credential-like data at {path}")


def _walk_leaves(value: Any, path: str = "$") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            found.extend(_walk_leaves(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_walk_leaves(item, f"{path}[{index}]"))
    else:
        found.append((path, value))
    return found


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _category_for_key(value: str) -> str | None:
    key = _normalize_key(value)
    if category := _SENSITIVE_KEYS.get(key):
        return category
    if "token" in key or "secret" in key:
        return "token"
    if "email" in key:
        return "email"
    if "address" in key or "postcode" in key or "postalcode" in key:
        return "address"
    if "supplypoint" in key or key in {"mpan", "spin"}:
        return "supply_point"
    if "meter" in key and ("serial" in key or key.endswith("id")):
        return "meter"
    if "register" in key and key.endswith("id"):
        return "register"
    if "device" in key and key.endswith("id"):
        return "device"
    if key.endswith("name"):
        return "name"
    if key.endswith("id") or key.endswith("identifier"):
        return "identifier"
    return None


def _looks_secret(value: str) -> bool:
    return bool(
        _AUTH_PATTERN.search(value)
        or _JWT_PATTERN.search(value)
        or _PRIVATE_KEY_PATTERN.search(value)
        or _LONG_SECRET_PATTERN.fullmatch(value)
    )
