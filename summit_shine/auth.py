"""Shared-password auth backed by a signed session cookie."""
from __future__ import annotations

import hmac
import hashlib
import os
import time

from fastapi import Request

SESSION_COOKIE = "summit_session"
_DEFAULT_SECRET = "dev-secret-change-me"
SECRET = os.environ.get("SUMMIT_SECRET", _DEFAULT_SECRET).encode()
PASSWORD = os.environ.get("SUMMIT_PASSWORD", "summit-shine")
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


def _sign(value: str) -> str:
    sig = hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def _verify(signed: str) -> str | None:
    if not signed or "." not in signed:
        return None
    value, _, sig = signed.rpartition(".")
    expected = hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()
    return value if hmac.compare_digest(expected, sig) else None


def login(password: str) -> str | None:
    if not password or not hmac.compare_digest(password, PASSWORD):
        return None
    return _sign(str(int(time.time())))


def is_authenticated(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    value = _verify(cookie)
    if not value:
        return False
    try:
        issued = int(value)
    except ValueError:
        return False
    return (time.time() - issued) < SESSION_TTL_SECONDS


def current_user(request: Request) -> str | None:
    return "signed-in" if is_authenticated(request) else None
