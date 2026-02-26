"""FastAPI dependencies for authentication and authorization.

Provides `get_current_user` which validates a Supabase JWT via the Supabase
auth server. Routes use `Depends(get_current_user)` to require auth.

Uses Supabase's auth.get_user() API to validate tokens server-side, which
works with both legacy HS256 and newer ECC (P-256) JWT signing keys.
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tablement.web import db

logger = logging.getLogger(__name__)

http_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(http_bearer),
) -> dict:
    """Validate Supabase JWT and return the user data.

    Uses Supabase's auth.get_user() to validate the token server-side.
    This works with any JWT signing algorithm (HS256, ES256, etc.)
    and doesn't require storing the JWT secret locally.

    Returns a dict with at minimum:
        - "id": user UUID (same as auth.users.id)

    Raises 401 if the token is missing or invalid.
    """
    if not creds:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        client = db.get_client()
        resp = await client.auth.get_user(creds.credentials)
        if not resp or not resp.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        # Return a dict with user info for downstream use
        return {
            "sub": resp.user.id,
            "id": resp.user.id,
            "phone": resp.user.phone,
            "email": resp.user.email,
            "role": resp.user.role,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.debug("Token validation failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_user_id(user: dict = Depends(get_current_user)) -> str:
    """Extract the user UUID from the validated user data."""
    user_id = user.get("sub") or user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token: no user ID")
    return user_id
