"""OpenTable auth routes — Bearer token linking.

Flow:
1. User extracts Bearer token from OT mobile app (via HTTP proxy)
2. POST /link-opentable  → validates token, stores encrypted in profiles
3. POST /validate-opentable-token → dry-run validation without storing

Separate from Resy auth (auth.py) — no shared logic.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from tablement.opentable.api import OpenTableApiClient
from tablement.opentable.models import OTAuthToken
from tablement.web import db
from tablement.web.deps import get_user_id
from tablement.web.encryption import encrypt_password
from tablement.web.schemas import LinkOpenTableRequest, LinkOpenTableResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/link-opentable", response_model=LinkOpenTableResponse)
async def link_opentable(
    body: LinkOpenTableRequest,
    user_id: str = Depends(get_user_id),
):
    """Link an OpenTable account by validating a Bearer token.

    The user extracts the Bearer token from the OpenTable mobile app
    (via Charles Proxy, mitmproxy, etc.), then pastes it here.
    We validate it against the OT API, extract identity data,
    encrypt and store it.
    """
    # Step 1: Validate the token against OT API
    token = OTAuthToken(
        bearer_token=body.bearer_token,
        diner_id=body.diner_id,
        gpid=body.gpid,
        phone=body.phone,
    )

    try:
        async with OpenTableApiClient(auth_token=token) as client:
            user_info = await client.validate_token()
    except Exception as exc:
        logger.warning("OT token validation failed for user %s: %s", user_id[:8], exc)
        raise HTTPException(
            status_code=400,
            detail="Invalid OpenTable token — it may have expired. Please extract a fresh token.",
        ) from exc

    # Step 2: Extract diner_id and gpid from validation response
    diner_id = user_info.get("diner_id", body.diner_id)
    gpid = user_info.get("gpid", body.gpid)
    phone = user_info.get("phone", body.phone)
    first_name = user_info.get("first_name", "")
    last_name = user_info.get("last_name", "")

    if not diner_id:
        raise HTTPException(
            status_code=400,
            detail="Could not extract diner ID from token. Please provide it manually.",
        )

    # Step 3: Encrypt and store in profiles
    encrypted_token = encrypt_password(body.bearer_token)

    await db.upsert_profile(
        user_id,
        opentable_linked=True,
        opentable_bearer_token_encrypted=encrypted_token,
        opentable_diner_id=diner_id,
        opentable_gpid=gpid,
        opentable_phone=phone,
    )

    diner_name = f"{first_name} {last_name}".strip() or None

    logger.info(
        "OT linked for user %s → diner_id=%s name=%s",
        user_id[:8],
        diner_id,
        diner_name,
    )

    return LinkOpenTableResponse(
        linked=True,
        diner_name=diner_name,
        diner_id=diner_id,
    )


@router.post("/validate-opentable-token")
async def validate_opentable_token(
    body: LinkOpenTableRequest,
    user_id: str = Depends(get_user_id),
):
    """Dry-run: validate an OT Bearer token without storing it.

    Use this for the frontend "Test Connection" button before linking.
    """
    token = OTAuthToken(
        bearer_token=body.bearer_token,
        diner_id=body.diner_id,
        gpid=body.gpid,
        phone=body.phone,
    )

    try:
        async with OpenTableApiClient(auth_token=token) as client:
            user_info = await client.validate_token()
    except Exception as exc:
        return {
            "valid": False,
            "error": str(exc),
        }

    return {
        "valid": True,
        "diner_id": user_info.get("diner_id", ""),
        "name": f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip(),
        "email": user_info.get("email", ""),
    }
