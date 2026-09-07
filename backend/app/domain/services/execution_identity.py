"""Keyed, privacy-preserving identities for execution provenance.

Private or administrator-controlled values must never be persisted directly or
fed to an ordinary, unkeyed hash.  This module is the only boundary that turns
those values into stable opaque identifiers for execution snapshots.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from app.core.config import get_settings


_KEY_DERIVATION_CONTEXT = b"ai-dataseek/execution-snapshot-identity-key/v1"
_VALUE_CONTEXT = b"ai-dataseek/execution-snapshot-private-value/v1\0"


class ExecutionIdentityConfigurationError(RuntimeError):
    """Raised when no stable secret is available for private identities."""


def private_identity_hmac(value: Any) -> str:
    """Return a stable HMAC-SHA256 without retaining or exposing ``value``.

    ``EXECUTION_SNAPSHOT_IDENTITY_KEY`` is the preferred deployment-owned key.
    Existing installations fall back to the already-required model ``API_KEY``
    so identities remain stable across process restarts before operators add a
    dedicated key.  The fallback is intentionally not an unkeyed digest.
    """

    settings = get_settings()
    configured_key = getattr(settings, "execution_snapshot_identity_key", None)
    fallback_key = getattr(settings, "api_key", None)
    raw_key = configured_key or fallback_key
    if not isinstance(raw_key, str) or not raw_key:
        raise ExecutionIdentityConfigurationError(
            "Execution snapshot identity requires a stable server secret"
        )

    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    # Derive a purpose-specific key so reuse of API_KEY during the compatibility
    # window cannot collide with any authentication or provider signature.
    identity_key = hmac.new(
        raw_key.encode("utf-8", errors="strict"),
        _KEY_DERIVATION_CONTEXT,
        hashlib.sha256,
    ).digest()
    return hmac.new(identity_key, _VALUE_CONTEXT + payload, hashlib.sha256).hexdigest()
