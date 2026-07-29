"""Privacy-preserving local identifiers for OEJP resources."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_STORAGE_KEY = f"{DOMAIN}.identity"
_STORAGE_VERSION = 1
_SECRET_BYTES = 32
_SECRET_LOCK = asyncio.Lock()


async def async_get_identity_secret(hass: HomeAssistant) -> str:
    """Load or create the installation-local HMAC secret."""
    store = Store[dict[str, str]](
        hass,
        _STORAGE_VERSION,
        _STORAGE_KEY,
        private=True,
    )
    async with _SECRET_LOCK:
        stored = await store.async_load()
        if isinstance(stored, dict):
            secret = stored.get("secret")
            if isinstance(secret, str) and _is_valid_secret(secret):
                return secret

        secret = secrets.token_hex(_SECRET_BYTES)
        await store.async_save({"secret": secret})
        return secret


def stable_account_identity(secret: str, account_number: str) -> str:
    """Create a stable installation-local identity for one OEJP account."""
    if not account_number.strip():
        raise ValueError("An account number is required")
    return stable_resource_identity(secret, "account", account_number)


def stable_resource_identity(secret: str, resource_type: str, provider_id: str) -> str:
    """Create a stable installation-local identity for a provider resource."""
    canonical_type = resource_type.strip().lower()
    canonical_id = provider_id.strip()
    if not canonical_type or not canonical_id:
        raise ValueError("A resource type and provider identifier are required")
    digest = hmac.new(
        bytes.fromhex(secret),
        f"{canonical_type}\0{canonical_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{canonical_type}-{digest}"


def stable_login_identity(secret: str, issuer: str, subject: str) -> str:
    """Create a stable installation-local identity for one OAuth login."""
    canonical_issuer = issuer.strip().rstrip("/")
    canonical_subject = subject.strip()
    if not canonical_issuer or not canonical_subject:
        raise ValueError("OAuth issuer and subject are required")
    digest = hmac.new(
        bytes.fromhex(secret),
        f"{canonical_issuer}\0{canonical_subject}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"login-{digest}"


def _is_valid_secret(secret: str) -> bool:
    try:
        return len(bytes.fromhex(secret)) == _SECRET_BYTES
    except ValueError:
        return False
