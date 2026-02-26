"""Auth API routes — phone OTP via Supabase + Resy credential linking.

Flow:
1. POST /send-otp        → sends SMS verification code to phone
2. POST /verify-otp      → verifies code, returns Supabase JWT
3. POST /link-resy       → stores encrypted Resy credentials
4. GET  /me              → returns user profile
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from tablement.api import ResyApiClient
from tablement.web import db
from tablement.web.deps import get_user_id
from tablement.web.encryption import encrypt_password
from tablement.web.schemas import (
    LinkResyRequest,
    LinkResyResponse,
    OtpSendRequest,
    OtpSendResponse,
    OtpVerifyRequest,
    OtpVerifyResponse,
    UserProfileResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/send-otp", response_model=OtpSendResponse)
async def send_otp(body: OtpSendRequest):
    """Send a 6-digit SMS verification code to the given phone number."""
    try:
        client = db.get_client()
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Authentication service not configured. Please set SUPABASE_URL and SUPABASE_ANON_KEY.",
        )

    redacted = body.phone[:5] + "***" + body.phone[-2:] if len(body.phone) > 7 else "***"
    logger.info("Sending OTP to %s", redacted)

    try:
        await client.auth.sign_in_with_otp(
            {
                "phone": body.phone,
                "options": {"should_create_user": True},
            }
        )
    except Exception as e:
        err = str(e).lower()
        logger.warning("OTP send failed for %s: %s", redacted, e)
        if "phone provider" in err or "not enabled" in err:
            raise HTTPException(status_code=503, detail="Phone auth is not enabled in Supabase. Enable the Phone provider in Authentication settings.")
        if "rate" in err or "limit" in err:
            raise HTTPException(status_code=429, detail="Too many requests. Please wait a minute before trying again.")
        raise HTTPException(status_code=400, detail=f"Failed to send verification code: {e}")

    return OtpSendResponse(sent=True, message="Verification code sent")


@router.post("/verify-otp", response_model=OtpVerifyResponse)
async def verify_otp(body: OtpVerifyRequest):
    """Verify the 6-digit code and return auth tokens."""
    try:
        client = db.get_client()
    except RuntimeError:
        raise HTTPException(
            status_code=503,
            detail="Authentication service not configured. Please set SUPABASE_URL and SUPABASE_ANON_KEY.",
        )

    redacted = body.phone[:5] + "***" + body.phone[-2:] if len(body.phone) > 7 else "***"
    try:
        resp = await client.auth.verify_otp(
            {
                "phone": body.phone,
                "token": body.code,
                "type": "sms",
            }
        )
    except Exception as e:
        err = str(e).lower()
        logger.warning("OTP verify failed for %s: %s", redacted, e)
        if "expired" in err:
            raise HTTPException(status_code=401, detail="Verification code has expired. Please request a new one.")
        if "invalid" in err or "token" in err:
            raise HTTPException(status_code=401, detail="Invalid verification code. Please check and try again.")
        raise HTTPException(status_code=401, detail=f"Verification failed: {e}")

    if not resp.session:
        raise HTTPException(status_code=401, detail="Verification failed — no session returned")

    # Ensure a profile row exists for this user
    user_id = resp.user.id
    profile = await db.get_profile(user_id)
    if not profile:
        await db.upsert_profile(user_id)

    return OtpVerifyResponse(
        access_token=resp.session.access_token,
        refresh_token=resp.session.refresh_token,
        user_id=user_id,
    )


@router.post("/link-resy", response_model=LinkResyResponse)
async def link_resy(
    body: LinkResyRequest,
    user_id: str = Depends(get_user_id),
):
    """Link a Resy account by verifying credentials and storing encrypted."""
    # Verify the Resy credentials actually work
    try:
        async with ResyApiClient() as client:
            auth_resp = await client.authenticate(body.email, body.password)
    except Exception as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                body_json = e.response.json()
                msg = body_json.get("message", msg)
            except Exception:
                pass
        raise HTTPException(status_code=401, detail=f"Resy login failed: {msg}")

    # Encrypt and store
    encrypted_pw = encrypt_password(body.password)
    await db.upsert_profile(
        user_id,
        resy_email=body.email,
        resy_password_encrypted=encrypted_pw,
    )

    return LinkResyResponse(
        linked=True,
        resy_first_name=auth_resp.first_name,
        resy_last_name=auth_resp.last_name,
    )


@router.get("/me", response_model=UserProfileResponse)
async def me(user_id: str = Depends(get_user_id)):
    """Return the current user's profile."""
    profile = await db.get_profile(user_id)
    if not profile:
        profile = await db.upsert_profile(user_id)

    return UserProfileResponse(
        user_id=user_id,
        resy_linked=bool(profile.get("resy_email")),
        resy_email=profile.get("resy_email"),
        stripe_linked=bool(profile.get("stripe_customer_id")),
    )
