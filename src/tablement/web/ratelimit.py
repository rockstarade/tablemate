"""Shared rate limiter instance — imported by routes and app."""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _get_user_key(request: Request) -> str:
    """Extract rate limit key from auth token or fall back to IP."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and len(auth) > 20:
        return f"user:{auth[7:27]}"
    return get_remote_address(request)


limiter = Limiter(key_func=_get_user_key)
