"""Webhook authentication: HMAC-SHA256 over "<timestamp>.<body>" with replay window."""
from __future__ import annotations

import hashlib
import hmac


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: str | None, signature: str | None, body: bytes,
           now_epoch: float, max_skew: int) -> bool:
    if not timestamp or not signature or not timestamp.isdigit():
        return False
    if abs(now_epoch - int(timestamp)) > max_skew:      # replay protection
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
