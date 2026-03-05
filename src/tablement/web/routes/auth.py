"""Auth API routes — phone OTP via Supabase + Resy credential linking.

Flow:
1. POST /send-otp        → sends SMS verification code to phone
2. POST /verify-otp      → verifies code, returns Supabase JWT
3. POST /link-resy       → stores encrypted Resy credentials
4. GET  /me              → returns user profile
"""

from __future__ import annotations

import logging
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from tablement.api import ResyApiClient
from tablement.web.ratelimit import limiter
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

# Dev mode token store (in-memory, lost on restart — that's fine for dev)
_dev_tokens: dict[str, str] = {}  # token -> user_id


@router.post("/send-otp", response_model=OtpSendResponse)
@limiter.limit("5/minute")
async def send_otp(request: Request, body: OtpSendRequest):
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
        raise HTTPException(status_code=400, detail="Failed to send verification code. Please try again.")

    return OtpSendResponse(sent=True, message="Verification code sent")


@router.post("/verify-otp", response_model=OtpVerifyResponse)
@limiter.limit("5/minute")
async def verify_otp(request: Request, body: OtpVerifyRequest):
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
        raise HTTPException(status_code=401, detail="Verification failed. Please try again.")

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
        logger.warning("Resy login failed for user %s: %s", user_id, msg)
        raise HTTPException(status_code=401, detail="Resy login failed. Please check your email and password.")

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


@router.post("/resy-send-otp")
@limiter.limit("5/minute")
async def resy_send_otp(request: Request, body: dict):
    """Send an OTP code to the user's phone via Resy's auth API."""
    phone = body.get("phone", "").strip()
    if not phone:
        raise HTTPException(400, "Phone number is required")

    # Ensure +1 prefix for US numbers
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        phone = f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        phone = f"+{digits}"
    elif not phone.startswith("+"):
        phone = f"+{digits}"

    redacted = phone[:5] + "***" + phone[-2:]
    logger.info("Sending Resy OTP to %s", redacted)

    try:
        async with ResyApiClient() as client:
            result = await client.send_phone_otp(phone)
    except Exception as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                msg = e.response.json().get("message", msg)
            except Exception:
                pass
        logger.warning("Resy OTP send failed for %s: %s", redacted, msg)
        raise HTTPException(400, f"Failed to send code: {msg}")

    return {"sent": True, "message": "Code sent to your phone via Resy"}


@router.post("/resy-verify-otp")
@limiter.limit("5/minute")
async def resy_verify_otp(request: Request, body: dict, user_id: str = Depends(get_user_id)):
    """Verify the Resy phone OTP code and link the account.

    May return a challenge requiring email verification (Resy security step
    for phones linked to existing accounts).
    """
    phone = body.get("phone", "").strip()
    code = body.get("code", "").strip()
    if not phone or not code:
        raise HTTPException(400, "Phone and code are required")

    # Normalize phone
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        phone = f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        phone = f"+{digits}"
    elif not phone.startswith("+"):
        phone = f"+{digits}"

    try:
        async with ResyApiClient() as client:
            result = await client.verify_phone_otp(phone, code)
    except Exception as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                msg = e.response.json().get("message", msg)
            except Exception:
                pass
        logger.warning("Resy OTP verify failed: %s", msg)
        raise HTTPException(401, f"Verification failed: {msg}")

    # If challenge returned, send it to frontend for email verification step
    if isinstance(result, dict) and "challenge" in result:
        challenge = result["challenge"]
        claim = result.get("mobile_claim", {})
        return {
            "linked": False,
            "challenge": True,
            "claim_token": claim.get("claim_token", ""),
            "challenge_id": challenge.get("challenge_id", ""),
            "challenge_message": challenge.get("message", "Please verify your email"),
            "first_name": challenge.get("first_name", ""),
        }

    # Direct auth success (no challenge needed)
    auth_resp = result
    await db.upsert_profile(
        user_id,
        resy_email=phone,
        resy_token=auth_resp.token,
    )
    return {
        "linked": True,
        "resy_first_name": auth_resp.first_name,
        "resy_last_name": auth_resp.last_name,
    }


@router.post("/resy-complete-challenge")
@limiter.limit("5/minute")
async def resy_complete_challenge(request: Request, body: dict, user_id: str = Depends(get_user_id)):
    """Complete the email challenge after phone OTP verification."""
    claim_token = body.get("claim_token", "").strip()
    challenge_id = body.get("challenge_id", "").strip()
    email = body.get("email", "").strip()
    if not claim_token or not challenge_id or not email:
        raise HTTPException(400, "claim_token, challenge_id, and email are required")

    try:
        async with ResyApiClient() as client:
            auth_resp = await client.complete_phone_challenge(claim_token, challenge_id, email)
    except Exception as e:
        msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                msg = e.response.json().get("message", msg)
            except Exception:
                pass
        logger.warning("Resy challenge failed: %s", msg)
        raise HTTPException(401, f"Email verification failed: {msg}")

    # Store credentials
    await db.upsert_profile(
        user_id,
        resy_email=email,
        resy_token=auth_resp.token,
    )
    return {
        "linked": True,
        "resy_first_name": auth_resp.first_name,
        "resy_last_name": auth_resp.last_name,
    }


@router.get("/me", response_model=UserProfileResponse)
async def me(user_id: str = Depends(get_user_id)):
    """Return the current user's profile."""
    # Dev users don't exist in Supabase auth.users, so skip DB entirely
    if user_id == "00000000-0000-0000-0000-000000000001":
        return UserProfileResponse(
            user_id=user_id,
            resy_linked=False,
            resy_email=None,
            stripe_linked=False,
        )

    profile = await db.get_profile(user_id)
    if not profile:
        profile = await db.upsert_profile(user_id)

    return UserProfileResponse(
        user_id=user_id,
        resy_linked=bool(profile.get("resy_email")),
        resy_email=profile.get("resy_email"),
        stripe_linked=bool(profile.get("stripe_customer_id")),
    )


# ---------------------------------------------------------------------------
# Dev Mode — skip phone auth during development
# ---------------------------------------------------------------------------


@router.get("/dev-mode")
async def dev_mode_check():
    """Check if dev mode is enabled. Frontend uses this to show the skip button."""
    return {"dev": os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")}


@router.post("/dev-skip")
async def dev_skip():
    """Create a quick-access session token without phone verification.

    Temporary admin convenience — skip phone auth and go straight to browse.
    """

    # Generate a deterministic dev user ID (same across restarts for convenience)
    # Must be a valid UUID since profiles.id is a uuid column
    dev_user_id = "00000000-0000-0000-0000-000000000001"
    dev_token = f"dev-token-{uuid.uuid4().hex[:16]}"

    # Store in memory
    _dev_tokens[dev_token] = dev_user_id

    # Note: dev user doesn't exist in Supabase auth.users, so we skip
    # profile creation. The /me endpoint handles dev users specially.

    logger.info("Dev skip: created token for user %s", dev_user_id)
    return {
        "access_token": dev_token,
        "user_id": dev_user_id,
    }
