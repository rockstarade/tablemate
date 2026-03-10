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
import secrets
import string
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

# Referral code alphabet (uppercase + digits, no ambiguous chars)
_REFERRAL_CHARS = string.ascii_uppercase + string.digits


def _generate_referral_code() -> str:
    """Generate a unique referral code like TP-K9X3M2."""
    suffix = "".join(secrets.choice(_REFERRAL_CHARS) for _ in range(6))
    return f"TP-{suffix}"


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

    # Check if phone is banned
    if await db.is_phone_banned(body.phone):
        raise HTTPException(status_code=403, detail="This phone number is not allowed.")

    # VoIP check via Twilio Lookup (block Google Voice, TextNow, etc.)
    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    twilio_auth = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if twilio_sid and twilio_auth:
        try:
            from twilio.rest import Client as TwilioClient
            twilio_client = TwilioClient(twilio_sid, twilio_auth)
            lookup = twilio_client.lookups.v2.phone_numbers(body.phone).fetch(
                fields="line_type_intelligence"
            )
            line_type = (lookup.line_type_intelligence or {}).get("type", "")
            logger.info("Twilio lookup %s: type=%s", redacted, line_type)
            if line_type == "voip":
                raise HTTPException(
                    status_code=400,
                    detail="VoIP numbers are not supported. Please use a mobile phone number.",
                )
        except HTTPException:
            raise
        except ImportError:
            logger.warning("twilio package not installed — skipping VoIP check")
        except Exception as e:
            # Don't block sign-up if Twilio lookup fails
            logger.warning("Twilio lookup failed for %s: %s", redacted, e)

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
    is_new_user = not profile
    if is_new_user:
        # Generate a unique referral code for this new user
        referral_code = _generate_referral_code()
        for _ in range(5):  # retry on collision
            existing = await db.get_profile_by_referral_code(referral_code)
            if not existing:
                break
            referral_code = _generate_referral_code()
        await db.upsert_profile(user_id, referral_code=referral_code, gifts_remaining=3)
        logger.info("New user %s — assigned referral code %s", user_id, referral_code)

        # Handle referral code redemption (new users only)
        ref_code = (body.referral_code or "").strip().upper()
        if ref_code:
            try:
                referrer = await db.get_profile_by_referral_code(ref_code)
                if referrer and referrer["id"] != user_id:
                    referrer_gifts = referrer.get("gifts_remaining", 0)
                    if referrer_gifts > 0:
                        # Deduct a referral slot from referrer
                        await db.decrement_gifts(referrer["id"])
                        # Enable 50% off first reservation for new user
                        await db.upsert_profile(user_id, referred_by=referrer["id"], referral_discount=True)
                        # Audit trail
                        await db.create_transaction(
                            user_id=user_id,
                            type="referral_received",
                            amount_cents=0,
                            credits_delta=0,
                            description=f"50% off first reservation from {ref_code}",
                        )
                        await db.create_transaction(
                            user_id=referrer["id"],
                            type="referral_sent",
                            amount_cents=0,
                            credits_delta=0,
                            description=f"Referral discount sent to new user (code {ref_code})",
                        )
                        logger.info(
                            "Referral redeemed: %s used code %s — referrer %s gifts now %d",
                            user_id, ref_code, referrer["id"], referrer_gifts - 1,
                        )
                    else:
                        logger.info("Referral code %s has no gifts remaining", ref_code)
                else:
                    logger.info("Invalid referral code %s (not found or self-referral)", ref_code)
            except Exception as e:
                logger.warning("Referral redemption failed for code %s: %s", ref_code, e)

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
async def resy_send_otp(request: Request, body: dict, user_id: str = Depends(get_user_id)):
    """DISABLED — Phone OTP cannot store password, which the sniper requires.
    Users must link via email+password instead.
    Re-enable when/if the sniper supports token-based auth.
    """
    raise HTTPException(
        400,
        "Phone OTP linking is temporarily disabled. "
        "Please use Email + Password to link your Resy account."
    )
    # ── Original implementation (kept for future reference) ──
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
        raise HTTPException(400, "Failed to send verification code. Please try again.")

    return {"sent": True, "message": "Code sent to your phone via Resy"}


@router.post("/resy-verify-otp")
@limiter.limit("5/minute")
async def resy_verify_otp(request: Request, body: dict, user_id: str = Depends(get_user_id)):
    """DISABLED — Phone OTP cannot store password, which the sniper requires."""
    raise HTTPException(
        400,
        "Phone OTP linking is temporarily disabled. "
        "Please use Email + Password to link your Resy account."
    )
    # ── Original implementation (kept for future reference) ──
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
        raise HTTPException(401, "Verification failed. Please check the code and try again.")

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
        resy_auth_token=auth_resp.token,
    )
    return {
        "linked": True,
        "resy_first_name": auth_resp.first_name,
        "resy_last_name": auth_resp.last_name,
    }


@router.post("/resy-complete-challenge")
@limiter.limit("5/minute")
async def resy_complete_challenge(request: Request, body: dict, user_id: str = Depends(get_user_id)):
    """DISABLED — Phone OTP flow disabled; challenge completion not needed."""
    raise HTTPException(
        400,
        "Phone OTP linking is temporarily disabled. "
        "Please use Email + Password to link your Resy account."
    )
    # ── Original implementation (kept for future reference) ──
    claim_token = body.get("claim_token", "").strip()
    challenge_id = body.get("challenge_id", "").strip()
    email = body.get("email", "").strip()
    phone = body.get("phone", "").strip()
    if not claim_token or not challenge_id or not email:
        raise HTTPException(400, "claim_token, challenge_id, and email are required")

    # Normalize phone if provided (Resy requires mobile_number on challenge completion)
    if phone:
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) == 10:
            phone = f"+1{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            phone = f"+{digits}"
        elif not phone.startswith("+"):
            phone = f"+{digits}"

    logger.info("resy-complete-challenge: email=%s, phone=%s, claim_token=%s..., challenge_id=%s...",
                email, phone[:5] + "***" if phone else "EMPTY", claim_token[:10], challenge_id[:10])

    try:
        async with ResyApiClient() as client:
            auth_resp = await client.complete_phone_challenge(claim_token, challenge_id, email, phone=phone)
    except Exception as e:
        msg = str(e) or repr(e)
        logger.warning("Resy challenge raw exception: type=%s, str=%s", type(e).__name__, msg[:300])
        if hasattr(e, "response") and e.response is not None:
            try:
                raw_text = e.response.text
                logger.warning("Resy challenge response body: %s", raw_text[:500])
                body = e.response.json()
                # Resy returns field-level errors: {"mobile_number": "..."} not {"message": "..."}
                if isinstance(body, dict):
                    extracted = body.get("message") or next(
                        (v for v in body.values() if isinstance(v, str)), None
                    )
                    if extracted:
                        msg = extracted
            except Exception:
                pass
        if not msg:
            msg = "Unknown error — check server logs"
        logger.warning("Resy challenge failed: %s", msg)
        raise HTTPException(401, "Email verification failed. Please check your email and try again.")

    # Store credentials
    await db.upsert_profile(
        user_id,
        resy_email=email,
        resy_auth_token=auth_resp.token,
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
        opentable_linked=bool(profile.get("opentable_linked")),
        opentable_diner_id=profile.get("opentable_diner_id"),
        stripe_linked=bool(profile.get("stripe_customer_id")),
        referral_code=profile.get("referral_code"),
        gifts_remaining=profile.get("gifts_remaining", 0),
        referral_discount=bool(profile.get("referral_discount", False)),
    )


# ---------------------------------------------------------------------------
# Location — capture user location for admin map
# ---------------------------------------------------------------------------


@router.post("/update-location")
async def update_location(body: dict, user_id: str = Depends(get_user_id)):
    """Store user's last known location (from browser geolocation)."""
    lat = body.get("lat")
    lng = body.get("lng")
    if lat is None or lng is None:
        raise HTTPException(400, "lat and lng required")
    city = body.get("city")
    await db.update_user_location(user_id, float(lat), float(lng), city)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Token Refresh — keep sessions alive without re-login
# ---------------------------------------------------------------------------


@router.post("/refresh")
@limiter.limit("10/minute")
async def refresh_session(request: Request, body: dict):
    """Refresh an expired access token using a refresh token.

    Frontend calls this when a 401 is received or proactively every ~50 min.
    Returns fresh access_token + refresh_token pair from Supabase.
    """
    refresh_token = (body.get("refresh_token") or "").strip()
    if not refresh_token:
        raise HTTPException(status_code=400, detail="refresh_token is required")

    try:
        client = db.get_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Auth service unavailable")

    try:
        resp = await client.auth.refresh_session(refresh_token)
    except Exception as e:
        logger.warning("Token refresh failed: %s", e)
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")

    if not resp or not resp.session:
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")

    logger.info("Token refreshed for user %s", resp.user.id if resp.user else "unknown")
    return {
        "access_token": resp.session.access_token,
        "refresh_token": resp.session.refresh_token,
    }


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

    Only available when DEV_MODE is enabled.
    """
    if os.environ.get("DEV_MODE", "").lower() not in ("true", "1", "yes"):
        raise HTTPException(status_code=403, detail="Dev mode is not enabled")

    # Generate a deterministic dev user ID (same across restarts for convenience)
    # Must be a valid UUID since profiles.id is a uuid column
    dev_user_id = "00000000-0000-0000-0000-000000000001"
    dev_token = f"dev-token-{uuid.uuid4().hex[:16]}"

    # Store in memory (bounded to prevent memory exhaustion)
    if len(_dev_tokens) > 100:
        _dev_tokens.clear()
    _dev_tokens[dev_token] = dev_user_id

    # Note: dev user doesn't exist in Supabase auth.users, so we skip
    # profile creation. The /me endpoint handles dev users specially.

    logger.info("Dev skip: created token for user %s", dev_user_id)
    return {
        "access_token": dev_token,
        "user_id": dev_user_id,
    }
