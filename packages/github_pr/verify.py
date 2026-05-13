"""Constant-time HMAC verification for GitHub webhook signatures."""

from __future__ import annotations

import hashlib
import hmac


def verify_hmac_sha256(
    secret: str,
    payload_bytes: bytes,
    signature_header: str | None,
) -> bool:
    """Verify a GitHub ``X-Hub-Signature-256`` header against the raw body.

    Uses :func:`hmac.compare_digest` to mitigate timing attacks. Returns False
    for any missing or malformed input — the caller should respond 401 and
    drop the request without writing any state.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_hex = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload_bytes,
        digestmod=hashlib.sha256,
    ).hexdigest()

    received_hex = signature_header.removeprefix("sha256=")

    return hmac.compare_digest(expected_hex, received_hex)
