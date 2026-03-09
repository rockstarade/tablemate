"""Admin panel API routes.

Protected by a simple ADMIN_PASSWORD env var. The admin dashboard
at /admin sends this password as an X-Admin-Token header.

Endpoints:
- GET /api/admin/stats     — system overview (users, reservations, jobs)
- GET /api/admin/users     — all user profiles
- GET /api/admin/reservations — all reservations across users
- POST /api/admin/reservations/{id}/cancel — force-cancel a reservation

VIP endpoints (admin ops command center):
- POST /api/admin/vip/snipe          — launch snipe from admin panel
- POST /api/admin/vip/test-auth      — test Resy login for a user
- POST /api/admin/vip/test-proxy     — test proxy connectivity
- POST /api/admin/vip/rotate-proxy   — force rotate proxy session
- GET  /api/admin/vip/proxy-status   — proxy config + active sessions
- POST /api/admin/vip/kill-all       — cancel all active jobs
- GET  /api/admin/vip/health         — system health (latencies, offsets)
- GET  /api/admin/vip/recent-results — last N completed snipes
- POST /api/admin/vip/lookup-url     — Resy URL → venue + policy
- PATCH /api/admin/vip/proxy-config  — switch proxy type
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time as time_module
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from sse_starlette.sse import EventSourceResponse

from tablement.api import ResyApiClient
from tablement.fingerprint import fingerprint_pool
from tablement.models import DropTime, SnipeConfig, TimePreference
from tablement.proxy import ProxyType, proxy_pool
from tablement.web import db
from tablement.web.encryption import decrypt_password, encrypt_password
from tablement.web.state import JobState, SnipePhase

logger = logging.getLogger(__name__)
router = APIRouter()

_admin_key_header = APIKeyHeader(name="X-Admin-Token", auto_error=False)


async def _require_admin(token: str | None = Depends(_admin_key_header)):
    """Verify the admin token matches ADMIN_PASSWORD env var."""
    expected = os.environ.get("ADMIN_PASSWORD", "")
    if not expected:
        raise HTTPException(503, "Admin panel not configured (set ADMIN_PASSWORD)")
    if token != expected:
        raise HTTPException(401, "Invalid admin token")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@router.get("/stats", dependencies=[Depends(_require_admin)])
async def stats(request: Request):
    """System overview."""
    # Active in-memory jobs
    job_manager = request.app.state.jobs
    active_jobs = job_manager.active_count

    # Scheduler info
    scheduler = request.app.state.scheduler
    scheduled_count = 0
    if scheduler:
        scheduled_count = len(scheduler.get_jobs())

    # DB aggregates
    svc = db.get_service_client()

    # Total users
    try:
        resp = await svc.table("profiles").select("id", count="exact").execute()
        total_users = resp.count if resp.count is not None else len(resp.data)
    except Exception:
        total_users = 0

    # Reservations by status
    try:
        resp = await svc.table("reservations").select("status").execute()
        status_counts: dict[str, int] = {}
        for row in resp.data:
            s = row["status"]
            status_counts[s] = status_counts.get(s, 0) + 1
        total_reservations = len(resp.data)
    except Exception:
        status_counts = {}
        total_reservations = 0

    # Proxy info
    proxy_stats = proxy_pool.get_stats() if proxy_pool.enabled else {"mode": "none"}

    return {
        "users": total_users,
        "reservations": {
            "total": total_reservations,
            "by_status": status_counts,
        },
        "jobs": {
            "active_in_memory": active_jobs,
            "scheduled": scheduled_count,
        },
        "proxy": proxy_stats,
        "server_time": datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users", dependencies=[Depends(_require_admin)])
async def list_users():
    """List all user profiles."""
    svc = db.get_service_client()
    try:
        resp = (
            await svc.table("profiles")
            .select("id, resy_email, stripe_customer_id, credits, gifts_remaining, referral_code, created_at, updated_at")
            .order("created_at", desc=True)
            .execute()
        )
        users = []
        for row in resp.data:
            users.append({
                "id": row["id"],
                "resy_email": row.get("resy_email"),
                "resy_linked": bool(row.get("resy_email")),
                "stripe_linked": bool(row.get("stripe_customer_id")),
                "credits": row.get("credits", 0),
                "gifts_remaining": row.get("gifts_remaining", 0),
                "referral_code": row.get("referral_code"),
                "created_at": str(row.get("created_at", "")),
            })
        return {"users": users, "total": len(users)}
    except Exception as e:
        logger.warning("Failed to list users: %s", e)
        return {"users": [], "total": 0, "error": str(e)}


@router.post("/users/{user_id}/credits", dependencies=[Depends(_require_admin)])
async def add_user_credits(user_id: str, body: dict):
    """Manually add credits to a user's balance."""
    amount = body.get("amount", 0)
    reason = body.get("reason", "Admin credit")
    if not isinstance(amount, int) or amount < 1 or amount > 100:
        raise HTTPException(400, "Amount must be 1-100")

    new_balance = await db.add_credits(user_id, amount)
    await db.create_transaction(
        user_id=user_id,
        type="admin_credit",
        amount_cents=0,
        credits_delta=amount,
        description=reason,
    )
    logger.info("Admin added %d credits to user %s — balance now %d", amount, user_id, new_balance)
    return {"success": True, "credits_added": amount, "new_balance": new_balance}


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


@router.get("/reservations", dependencies=[Depends(_require_admin)])
async def list_all_reservations():
    """List all reservations across all users."""
    svc = db.get_service_client()
    try:
        resp = (
            await svc.table("reservations")
            .select("*")
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        reservations = []
        for row in resp.data:
            reservations.append({
                "id": row["id"],
                "user_id": row["user_id"],
                "venue_id": row["venue_id"],
                "venue_name": row["venue_name"],
                "party_size": row["party_size"],
                "target_date": str(row["target_date"]),
                "mode": row["mode"],
                "status": row["status"],
                "time_preferences": row.get("time_preferences"),
                "resy_token": row.get("resy_token"),
                "attempts": row.get("attempts", 0),
                "elapsed_seconds": row.get("elapsed_seconds"),
                "error": row.get("error"),
                "created_at": str(row.get("created_at", "")),
            })
        return {"reservations": reservations, "total": len(reservations)}
    except Exception as e:
        logger.warning("Failed to list reservations: %s", e)
        return {"reservations": [], "total": 0, "error": str(e)}


@router.post("/reservations/{reservation_id}/cancel", dependencies=[Depends(_require_admin)])
async def admin_cancel_reservation(reservation_id: str, request: Request):
    """Force-cancel a reservation (admin)."""
    svc = db.get_service_client()

    # Get the reservation
    try:
        resp = (
            await svc.table("reservations")
            .select("*")
            .eq("id", reservation_id)
            .execute()
        )
        if not resp.data:
            raise HTTPException(404, "Reservation not found")
        row = resp.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")

    if row["status"] in ("confirmed", "cancelled"):
        raise HTTPException(400, f"Cannot cancel reservation with status '{row['status']}'")

    # Cancel in-memory job
    job_manager = request.app.state.jobs
    job = job_manager.get(reservation_id)
    if job:
        job.broadcast("snipe_result", {"success": False, "error": "Cancelled by admin"})
        job_manager.remove(reservation_id)

    # Cancel APScheduler job
    scheduler = request.app.state.scheduler
    if scheduler:
        for prefix in ("snipe_", "monitor_"):
            try:
                scheduler.remove_job(f"{prefix}{reservation_id}")
            except Exception:
                pass

    # Update DB
    await svc.table("reservations").update({
        "status": "cancelled",
        "error": "Cancelled by admin",
    }).eq("id", reservation_id).execute()

    # Release Stripe hold
    stripe_pi = row.get("stripe_payment_intent_id")
    if stripe_pi:
        try:
            import stripe
            stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
            stripe.PaymentIntent.cancel(stripe_pi)
        except Exception as e:
            logger.warning("Failed to release Stripe hold: %s", e)

    return {"cancelled": True, "reservation_id": reservation_id}


# ===========================================================================
# VIP CONTROL PANEL ENDPOINTS
# ===========================================================================


# ---------------------------------------------------------------------------
# Snipe Launcher
# ---------------------------------------------------------------------------


@router.post("/vip/snipe", dependencies=[Depends(_require_admin)])
async def vip_launch_snipe(body: dict, request: Request):
    """Launch a snipe or monitor from the admin VIP panel.

    Accepts the same fields as ReservationCreateRequest but also:
    - user_id: which user's Resy credentials to use (impersonate)
    - proxy_override: "residential" | "dedicated" | "direct"
    """
    from tablement.web.routes.reservations import (
        _build_snipe_config,
        _run_snipe,
        _schedule_monitor,
    )
    from tablement.web.schemas import DropTimeIn, ReservationCreateRequest, TimePreferenceIn

    user_id = body.get("user_id")
    if not user_id:
        raise HTTPException(400, "user_id is required (which account to use)")

    profile = await db.get_profile(user_id)
    if not profile or not profile.get("resy_email"):
        raise HTTPException(400, f"User {user_id[:8]}... has no linked Resy account")

    # Build the request
    time_prefs = [
        TimePreferenceIn(time=tp["time"], seating_type=tp.get("seating_type"))
        for tp in body.get("time_preferences", [])
    ]
    if not time_prefs:
        raise HTTPException(400, "At least one time preference is required")

    drop_time = None
    if body.get("mode") == "snipe" and body.get("drop_time"):
        dt = body["drop_time"]
        drop_time = DropTimeIn(
            hour=dt.get("hour", 10),
            minute=dt.get("minute", 0),
            second=dt.get("second", 0),
            timezone=dt.get("timezone", "America/New_York"),
            days_ahead=dt.get("days_ahead", 30),
        )

    req = ReservationCreateRequest(
        venue_id=body["venue_id"],
        venue_name=body.get("venue_name", "Unknown"),
        party_size=body.get("party_size", 2),
        date=body["date"],
        mode=body.get("mode", "snipe"),
        time_preferences=time_prefs,
        drop_time=drop_time,
        dry_run=body.get("dry_run", False),
    )

    # Create DB row (using service client, not user-scoped)
    time_prefs_json = [tp.model_dump() for tp in req.time_preferences]
    drop_time_json = req.drop_time.model_dump() if req.drop_time else None
    initial_status = "scheduled" if req.mode == "snipe" else "monitoring"

    row = await db.create_reservation(
        user_id=user_id,
        venue_id=req.venue_id,
        venue_name=req.venue_name,
        party_size=req.party_size,
        target_date=req.date,
        mode=req.mode,
        status=initial_status,
        time_preferences=time_prefs_json,
        drop_time_config=drop_time_json,
    )

    reservation_id = row["id"]

    # Store proxy override on the job state
    proxy_override = body.get("proxy_override")

    config = _build_snipe_config(req)
    # Apply window_minutes override from admin panel
    window_minutes = body.get("window_minutes")
    if window_minutes is not None:
        config.window_minutes = int(window_minutes)

    job = request.app.state.jobs.create(reservation_id, user_id)

    if req.mode == "snipe":
        job.task = asyncio.create_task(
            _run_snipe(request.app, reservation_id, user_id, config, profile, req.dry_run, job)
        )
    else:
        _schedule_monitor(request.app, reservation_id, user_id, req, profile)

    return {
        "launched": True,
        "reservation_id": reservation_id,
        "mode": req.mode,
        "dry_run": req.dry_run,
        "user_id": user_id,
    }


@router.get("/vip/snipe/{reservation_id}/events")
async def vip_snipe_events(reservation_id: str, request: Request, token: str = ""):
    """SSE stream for real-time updates on an admin-launched snipe.

    Uses query param ?token= for auth since EventSource doesn't support custom headers.
    Token is validated as either the admin password or an HMAC signature.
    """
    import hashlib
    import hmac
    expected = os.environ.get("ADMIN_PASSWORD", "")
    if not expected:
        raise HTTPException(401, "Admin not configured")
    # Accept direct password OR HMAC-signed token (hmac:timestamp:sig)
    valid = False
    if token == expected:
        valid = True
    elif token.startswith("hmac:"):
        parts = token.split(":", 2)
        if len(parts) == 3:
            _, ts, sig = parts
            msg = f"sse:{reservation_id}:{ts}".encode()
            expected_sig = hmac.new(expected.encode(), msg, hashlib.sha256).hexdigest()[:16]
            valid = hmac.compare_digest(sig, expected_sig)
    if not valid:
        raise HTTPException(401, "Invalid admin token")
    job_manager = request.app.state.jobs
    job = job_manager.get(reservation_id)

    if not job:
        # Check DB for completed reservation
        svc = db.get_service_client()
        resp = await svc.table("reservations").select("*").eq("id", reservation_id).execute()
        if not resp.data:
            raise HTTPException(404, "Reservation not found")
        row = resp.data[0]

        async def static_response():
            yield {
                "event": "snipe_phase",
                "data": json.dumps({
                    "phase": row.get("status", "idle"),
                    "message": row.get("error", ""),
                    "attempt": row.get("attempts", 0),
                }),
            }
            if row.get("status") in ("confirmed", "failed", "cancelled", "dry_run"):
                yield {
                    "event": "snipe_result",
                    "data": json.dumps({
                        "success": row["status"] in ("confirmed", "dry_run"),
                        "dry_run": row["status"] == "dry_run",
                        "attempts": row.get("attempts", 0),
                        "elapsed_seconds": row.get("elapsed_seconds", 0),
                        "error": row.get("error"),
                    }),
                }

        return EventSourceResponse(static_response())

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    job.event_queues.append(queue)

    async def generate():
        try:
            s = job.status
            yield {
                "event": "snipe_phase",
                "data": json.dumps({
                    "phase": s.phase.value,
                    "message": s.message,
                    "attempt": s.attempt,
                }),
            }
            while True:
                msg = await queue.get()
                yield {
                    "event": msg["event"],
                    "data": json.dumps(msg["data"]),
                }
                if msg["event"] == "snipe_result":
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if queue in job.event_queues:
                job.event_queues.remove(queue)

    return EventSourceResponse(generate())


# ---------------------------------------------------------------------------
# Token & Auth Manager
# ---------------------------------------------------------------------------


@router.post("/vip/test-auth", dependencies=[Depends(_require_admin)])
async def vip_test_auth(body: dict):
    """Test Resy login for a specific user. Returns token + payment methods."""
    user_id = body.get("user_id")
    if not user_id:
        raise HTTPException(400, "user_id is required")

    profile = await db.get_profile(user_id)
    if not profile or not profile.get("resy_email"):
        raise HTTPException(400, "User has no linked Resy account")

    resy_email = profile["resy_email"]
    resy_password = decrypt_password(profile["resy_password_encrypted"])

    try:
        async with ResyApiClient(user_id=user_id) as client:
            start = time_module.monotonic()
            auth_resp = await client.authenticate(resy_email, resy_password)
            elapsed = time_module.monotonic() - start

            payment_methods = [
                {"id": pm.id, "display": pm.display, "is_default": pm.is_default}
                for pm in auth_resp.payment_methods
            ]

            return {
                "success": True,
                "user_id": user_id,
                "resy_email": resy_email,
                "resy_user_id": auth_resp.id,
                "first_name": auth_resp.first_name,
                "last_name": auth_resp.last_name,
                "token_preview": auth_resp.token[:20] + "...",
                "payment_methods": payment_methods,
                "latency_ms": round(elapsed * 1000),
            }

    except Exception as e:
        return {
            "success": False,
            "user_id": user_id,
            "resy_email": resy_email,
            "error": str(e),
        }


@router.post("/vip/add-resy-account", dependencies=[Depends(_require_admin)])
async def vip_add_resy_account(body: dict):
    """Add a Resy account directly from the admin panel.

    Verifies credentials against Resy, creates a profile row, and stores encrypted password.
    This bypasses the normal user auth flow (phone OTP) for admin convenience.
    """
    import uuid

    resy_email = body.get("email", "").strip()
    resy_password = body.get("password", "").strip()

    if not resy_email or not resy_password:
        raise HTTPException(400, "email and password are required")

    # Check if this Resy email is already linked to a profile
    svc = db.get_service_client()
    existing = (
        await svc.table("profiles")
        .select("id, resy_email")
        .eq("resy_email", resy_email)
        .execute()
    )
    if existing.data:
        raise HTTPException(409, f"Resy account {resy_email} is already linked to user {existing.data[0]['id'][:8]}...")

    # Verify credentials against Resy
    try:
        async with ResyApiClient() as client:
            start = time_module.monotonic()
            auth_resp = await client.authenticate(resy_email, resy_password)
            elapsed = time_module.monotonic() - start
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error("Resy auth error for %s: %s\n%s", resy_email, e, traceback.format_exc())
        msg = str(e)
        # HTTP errors from Resy (bad creds, rate limit, etc.)
        if hasattr(e, "response") and e.response is not None:
            try:
                body_json = e.response.json()
                msg = body_json.get("message", msg)
            except Exception:
                pass
            raise HTTPException(401, f"Resy login failed: {msg}")
        # Connection / proxy errors
        if "connect" in msg.lower() or "timeout" in msg.lower() or "proxy" in msg.lower():
            raise HTTPException(502, f"Could not reach Resy API (proxy/network issue): {msg}")
        # Pydantic validation errors (response format mismatch)
        if "validation" in msg.lower() or "field required" in msg.lower():
            raise HTTPException(500, f"Resy responded but format unexpected — check server logs: {msg[:200]}")
        raise HTTPException(401, f"Resy login failed: {msg}")

    # Create a Supabase auth user first, then create the profile.
    # The profiles table has a FK to auth.users, so we can't just insert
    # a random UUID — we need a real auth user entry.
    try:
        svc_auth = db.get_service_client()
        create_resp = await svc_auth.auth.admin.create_user({
            "email": resy_email,
            "email_confirm": True,  # auto-confirm so the user is active
            "user_metadata": {"source": "admin_vip", "resy_linked": True},
        })
        user_id = create_resp.user.id
        logger.info("Created Supabase auth user %s for %s", user_id, resy_email)
    except Exception as e:
        err_msg = str(e)
        # If user already exists in auth.users, look up their ID
        if "already" in err_msg.lower() or "duplicate" in err_msg.lower() or "unique" in err_msg.lower():
            # Try to find the existing auth user by email
            try:
                users_resp = await svc_auth.auth.admin.list_users()
                found = None
                for u in users_resp:
                    # users_resp may be a list or have a .users attribute
                    user_list = u if isinstance(u, list) else [u]
                    for user in user_list:
                        if hasattr(user, "email") and user.email == resy_email:
                            found = user
                            break
                    if found:
                        break
                if found:
                    user_id = found.id
                    logger.info("Reusing existing auth user %s for %s", user_id, resy_email)
                else:
                    raise HTTPException(500, f"Auth user exists but could not be found: {err_msg}")
            except HTTPException:
                raise
            except Exception as lookup_err:
                raise HTTPException(500, f"Failed to look up existing auth user: {lookup_err}")
        else:
            logger.error("Failed to create Supabase auth user: %s", e)
            raise HTTPException(500, f"Failed to create user account: {err_msg}")

    encrypted_pw = encrypt_password(resy_password)
    try:
        await db.upsert_profile(
            user_id,
            resy_email=resy_email,
            resy_password_encrypted=encrypted_pw,
        )
    except Exception as e:
        logger.error("Failed to upsert profile for %s: %s", user_id, e)
        raise HTTPException(500, f"Profile creation failed: {e}")

    payment_methods = [
        {"id": pm.id, "display": pm.display, "is_default": pm.is_default}
        for pm in auth_resp.payment_methods
    ]

    return {
        "success": True,
        "user_id": user_id,
        "resy_email": resy_email,
        "first_name": auth_resp.first_name,
        "last_name": auth_resp.last_name,
        "payment_methods": payment_methods,
        "latency_ms": round(elapsed * 1000),
    }


@router.get("/vip/users-with-resy", dependencies=[Depends(_require_admin)])
async def vip_list_resy_users():
    """List all users that have linked Resy accounts (for the impersonate dropdown)."""
    svc = db.get_service_client()
    resp = (
        await svc.table("profiles")
        .select("id, resy_email, created_at")
        .neq("resy_email", None)
        .order("created_at", desc=True)
        .execute()
    )
    users = [
        {"id": row["id"], "resy_email": row.get("resy_email"), "created_at": str(row.get("created_at", ""))}
        for row in resp.data
        if row.get("resy_email")
    ]
    return {"users": users}


# ---------------------------------------------------------------------------
# Proxy Control
# ---------------------------------------------------------------------------


@router.get("/vip/proxy-status", dependencies=[Depends(_require_admin)])
async def vip_proxy_status():
    """Get current proxy configuration and active sessions."""
    stats = proxy_pool.get_stats()
    stats["available_types"] = [t.value for t in ProxyType]
    return stats


@router.post("/vip/test-proxy", dependencies=[Depends(_require_admin)])
async def vip_test_proxy(body: dict = None):
    """Test proxy connectivity and measure latency.

    Optional body: {"proxy_type": "residential" | "dedicated" | "direct"}
    """
    body = body or {}
    raw_type = body.get("proxy_type")
    proxy_type = ProxyType(raw_type) if raw_type else None

    result = await proxy_pool.test_connectivity(proxy_type)
    return result


@router.post("/vip/rotate-proxy", dependencies=[Depends(_require_admin)])
async def vip_rotate_proxy(body: dict):
    """Force-rotate proxy session for a user (get a new IP)."""
    user_id = body.get("user_id", "admin-test")
    new_url = proxy_pool.rotate_session(user_id)
    return {
        "rotated": True,
        "user_id": user_id,
        "new_proxy_url_masked": _mask_proxy_url_safe(new_url),
    }


@router.patch("/vip/proxy-config", dependencies=[Depends(_require_admin)])
async def vip_update_proxy_config(body: dict):
    """Switch the active proxy type at runtime."""
    raw_type = body.get("proxy_type")
    if not raw_type:
        raise HTTPException(400, "proxy_type is required")
    try:
        new_type = ProxyType(raw_type)
    except ValueError:
        raise HTTPException(400, f"Invalid proxy type: {raw_type}. Must be one of: {[t.value for t in ProxyType]}")

    proxy_pool.set_proxy_type(new_type)
    return {"updated": True, "proxy_type": new_type.value}


def _mask_proxy_url_safe(url: str | None) -> str | None:
    if not url:
        return None
    import re
    return re.sub(r"://([^@]+)@", "://***:***@", url)


# ---------------------------------------------------------------------------
# Live Ops Dashboard
# ---------------------------------------------------------------------------


@router.post("/vip/kill-all", dependencies=[Depends(_require_admin)])
async def vip_kill_all(request: Request):
    """Cancel ALL active jobs instantly (kill switch)."""
    job_manager = request.app.state.jobs
    count = job_manager.active_count

    # Broadcast cancellation to all SSE clients
    for job in list(job_manager._jobs.values()):
        job.broadcast("snipe_result", {"success": False, "error": "Killed by admin"})

    job_manager.cancel_all()

    # Also clear APScheduler jobs
    scheduler = request.app.state.scheduler
    scheduled_cleared = 0
    if scheduler:
        for j in scheduler.get_jobs():
            j.remove()
            scheduled_cleared += 1

    # Update DB: set all active reservations to cancelled
    svc = db.get_service_client()
    try:
        await (
            svc.table("reservations")
            .update({"status": "cancelled", "error": "Killed by admin"})
            .in_("status", ["scheduled", "monitoring", "sniping", "pending"])
            .execute()
        )
    except Exception as e:
        logger.warning("Failed to update DB for kill-all: %s", e)

    return {
        "killed": True,
        "jobs_cancelled": count,
        "scheduled_cleared": scheduled_cleared,
    }


@router.get("/vip/health", dependencies=[Depends(_require_admin)])
async def vip_health(request: Request):
    """System health: proxy latency, Resy API response time, NTP offset."""
    results = {}

    # Test Resy API latency (direct)
    try:
        async with ResyApiClient() as client:
            start = time_module.monotonic()
            await client.ping()
            results["resy_latency_ms"] = round((time_module.monotonic() - start) * 1000)
    except Exception as e:
        results["resy_latency_ms"] = None
        results["resy_error"] = str(e)

    # NTP offset
    try:
        from tablement.scheduler import PrecisionScheduler
        sched = PrecisionScheduler()
        ntp_offset = await sched.check_ntp_offset_async()
        results["ntp_offset_ms"] = round(ntp_offset * 1000, 1) if ntp_offset else None
    except Exception:
        results["ntp_offset_ms"] = None

    # Active jobs
    job_manager = request.app.state.jobs
    results["active_jobs"] = job_manager.active_count

    # Proxy info
    results["proxy"] = proxy_pool.get_stats()

    # Server uptime
    results["server_time"] = datetime.utcnow().isoformat() + "Z"

    return results


@router.get("/vip/recent-results", dependencies=[Depends(_require_admin)])
async def vip_recent_results(limit: int = 20):
    """Get the last N completed snipe/monitor results with timing breakdown."""
    svc = db.get_service_client()
    try:
        resp = (
            await svc.table("reservations")
            .select("*")
            .in_("status", ["confirmed", "failed", "cancelled"])
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        results = []
        for row in resp.data:
            results.append({
                "id": row["id"],
                "user_id": row["user_id"],
                "venue_id": row["venue_id"],
                "venue_name": row["venue_name"],
                "target_date": str(row["target_date"]),
                "party_size": row["party_size"],
                "mode": row["mode"],
                "status": row["status"],
                "attempts": row.get("attempts", 0),
                "elapsed_seconds": row.get("elapsed_seconds"),
                "error": row.get("error"),
                "resy_token": row.get("resy_token"),
                "time_preferences": row.get("time_preferences"),
                "created_at": str(row.get("created_at", "")),
            })
        return {"results": results, "total": len(results)}
    except Exception as e:
        return {"results": [], "total": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# Venue Seating Types (smart detection for snipe form)
# ---------------------------------------------------------------------------


@router.post("/vip/seating-types", dependencies=[Depends(_require_admin)])
async def vip_seating_types(body: dict):
    """Fetch available seating types for a venue by checking real slot data.

    Calls find_slots for the given venue+date+party_size and extracts unique
    seating types (e.g. "Dining Room", "Bar", "Patio"). Returns an empty list
    if no slots are found (restaurant hasn't released dates yet, etc.).

    The frontend uses this to show a dropdown of available seating types
    instead of a freeform text input.
    """
    venue_id = body.get("venue_id")
    party_size = body.get("party_size", 2)
    target_date = body.get("date")

    if not venue_id:
        raise HTTPException(400, "venue_id is required")

    # Try multiple dates: the requested date, plus today and tomorrow
    # (in case the target date has no slots yet because it hasn't dropped)
    dates_to_try = []
    if target_date:
        dates_to_try.append(target_date)

    from datetime import date as date_type
    today = date_type.today()
    # Also check near-future dates that are likely already released
    for delta in [0, 1, 2, 7]:
        d = (today + timedelta(days=delta)).isoformat()
        if d not in dates_to_try:
            dates_to_try.append(d)

    seating_types = set()
    try:
        async with ResyApiClient(scout=True, user_id="admin-seating-probe") as client:
            for day_str in dates_to_try:
                try:
                    slots = await client.find_slots(venue_id, day_str, party_size)
                    for slot in slots:
                        if slot.config.type:
                            seating_types.add(slot.config.type)
                    if seating_types:
                        break  # Got types, no need to check more dates
                except Exception:
                    continue
    except Exception as e:
        logger.warning("Seating type probe failed for venue %s: %s", venue_id, e)
        return {"seating_types": [], "error": str(e)}

    # Sort for consistent display
    sorted_types = sorted(seating_types)
    return {
        "seating_types": sorted_types,
        "venue_id": venue_id,
    }


# ---------------------------------------------------------------------------
# Restaurant Intelligence
# ---------------------------------------------------------------------------


@router.post("/vip/lookup-url", dependencies=[Depends(_require_admin)])
async def vip_lookup_url(body: dict):
    """Resy URL → venue ID + booking policy extraction."""
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "url is required")

    from tablement.venue import VenueLookup, parse_resy_url
    from tablement.web.ai import detect_policy_with_ai

    venue_lookup = VenueLookup()

    try:
        parsed = parse_resy_url(url)
        venue_id, venue_name = await venue_lookup.from_url(url)
    except Exception as e:
        raise HTTPException(422, f"Could not look up venue: {e}")

    # Detect booking policy
    detected_policy = None
    url_slug = parsed.get("slug", "")
    location_slug = parsed.get("location", "")

    try:
        text = await venue_lookup.fetch_venue_content(url_slug, location_slug or None)
        if text:
            regex_result = venue_lookup.scrape_booking_policy(text)
            if regex_result:
                detected_policy = {
                    "days_ahead": regex_result.days_ahead,
                    "hour": regex_result.hour,
                    "minute": regex_result.minute,
                    "timezone": regex_result.timezone,
                    "source": "regex",
                    "confidence": "high",
                }
            else:
                ai_result = await detect_policy_with_ai(text, venue_name)
                if ai_result.detected and ai_result.days_ahead is not None:
                    detected_policy = {
                        "days_ahead": ai_result.days_ahead,
                        "hour": ai_result.hour,
                        "minute": ai_result.minute or 0,
                        "timezone": ai_result.timezone,
                        "source": "ai",
                        "confidence": ai_result.confidence,
                        "reasoning": ai_result.reasoning,
                    }
    except Exception:
        pass

    return {
        "venue_id": venue_id,
        "venue_name": venue_name,
        "detected_policy": detected_policy,
        "url_date": parsed.get("date"),
        "url_seats": parsed.get("seats"),
    }


# ---------------------------------------------------------------------------
# Drop Intelligence — tracking, observations, velocity data
# ---------------------------------------------------------------------------


@router.get("/vip/drop-intel/venues", dependencies=[Depends(_require_admin)])
async def drop_intel_list_venues():
    """List all curated venues (source of truth for drop intelligence)."""
    try:
        restaurants = await db.list_curated_restaurants(active_only=False)
        # Map to the format the admin panel expects
        venues = []
        for r in restaurants:
            venues.append({
                "venue_id": r["venue_id"],
                "venue_name": r.get("name", ""),
                "drop_hour": r.get("drop_hour", 10),
                "drop_minute": r.get("drop_minute", 0),
                "drop_timezone": "America/New_York",
                "days_ahead": r.get("drop_days_ahead", 30),
                "party_size": 2,
                "active": r.get("is_active", True),
            })
        return {"venues": venues}
    except Exception as e:
        return {"venues": [], "error": str(e)}


@router.get("/vip/drop-intel/observations", dependencies=[Depends(_require_admin)])
async def drop_intel_observations(venue_id: int | None = None, limit: int = 50):
    """Get drop observations. Optionally filter by venue_id."""
    if venue_id:
        data = await db.get_drop_observations(venue_id, limit=limit)
    else:
        data = await db.get_all_drop_observations(limit=limit)
    return {"observations": data, "total": len(data)}


@router.get("/vip/drop-intel/velocity", dependencies=[Depends(_require_admin)])
async def drop_intel_velocity(venue_id: int, target_date: str):
    """Get slot velocity data (snapshots over time) for a venue+date."""
    snapshots = await db.get_slot_snapshots(venue_id, target_date)
    return {"snapshots": snapshots, "total": len(snapshots)}


@router.get("/vip/drop-intel/summary", dependencies=[Depends(_require_admin)])
async def drop_intel_summary():
    """Get aggregate summary stats across all tracked venues.

    Returns per-venue: avg offset, median offset, avg slots, slot velocity.
    This is the core competitive intelligence dashboard data.
    """
    observations = await db.get_all_drop_observations(limit=500)

    # Group by venue
    by_venue: dict[int, list[dict]] = {}
    for obs in observations:
        vid = obs["venue_id"]
        by_venue.setdefault(vid, []).append(obs)

    summaries = []
    for vid, obs_list in by_venue.items():
        offsets = [o["offset_ms"] for o in obs_list if o.get("offset_ms") is not None]
        slot_counts = [o["slots_found"] for o in obs_list if o.get("slots_found")]

        if not offsets:
            continue

        offsets.sort()
        median_offset = offsets[len(offsets) // 2]

        summaries.append({
            "venue_id": vid,
            "venue_name": obs_list[0].get("venue_name", "Unknown"),
            "observation_count": len(obs_list),
            "avg_offset_ms": round(sum(offsets) / len(offsets), 1),
            "median_offset_ms": round(median_offset, 1),
            "min_offset_ms": round(min(offsets), 1),
            "max_offset_ms": round(max(offsets), 1),
            "avg_slots": round(sum(slot_counts) / len(slot_counts), 1) if slot_counts else 0,
            "slot_types": list({
                t for o in obs_list
                for t in (o.get("slot_types") or [])
            }),
            "latest_drop": obs_list[0].get("actual_drop_at"),
        })

    summaries.sort(key=lambda s: s["observation_count"], reverse=True)
    return {"summaries": summaries, "total_venues": len(summaries)}


# ---------------------------------------------------------------------------
# Slot Claims — multi-user conflict detection
# ---------------------------------------------------------------------------


@router.get("/vip/claims", dependencies=[Depends(_require_admin)])
async def list_claims(venue_id: int | None = None, target_date: str | None = None):
    """List active slot claims. Filter by venue and/or date."""
    if venue_id and target_date:
        claims = await db.get_active_claims(venue_id, target_date)
    else:
        # Get all active claims
        svc = db.get_service_client()
        q = svc.table("slot_claims").select("*").eq("status", "active")
        if venue_id:
            q = q.eq("venue_id", venue_id)
        if target_date:
            q = q.eq("target_date", target_date)
        resp = await q.order("created_at", desc=True).limit(100).execute()
        claims = resp.data
    return {"claims": claims, "total": len(claims)}


@router.post("/vip/claims/check", dependencies=[Depends(_require_admin)])
async def check_conflicts(body: dict):
    """Check if any slot claims conflict with a proposed booking.

    Returns conflicting claims (other users who want the same time window).
    """
    venue_id = body.get("venue_id")
    target_date = body.get("target_date")
    preferred_time = body.get("preferred_time")  # HH:MM
    window_minutes = body.get("window_minutes", 30)

    if not venue_id or not target_date:
        raise HTTPException(400, "venue_id and target_date required")

    claims = await db.get_active_claims(venue_id, target_date)

    conflicts = []
    if preferred_time:
        from datetime import time as time_type
        pref_h, pref_m = map(int, preferred_time.split(":"))
        pref_minutes = pref_h * 60 + pref_m

        for claim in claims:
            claim_h, claim_m = map(int, claim["preferred_time"].split(":"))
            claim_minutes = claim_h * 60 + claim_m
            # Check if windows overlap
            claim_window = claim.get("window_minutes", 30)
            max_window = max(window_minutes, claim_window)
            if abs(pref_minutes - claim_minutes) <= max_window:
                conflicts.append(claim)

    return {
        "conflicts": conflicts,
        "has_conflicts": len(conflicts) > 0,
        "total_claims": len(claims),
    }


@router.post("/vip/claims/create", dependencies=[Depends(_require_admin)])
async def create_claim(body: dict):
    """Register a slot claim when a snipe is launched.

    This creates a record so we can detect conflicts between multiple users
    targeting the same venue/date/time window.
    """
    user_id = body.get("user_id")
    venue_id = body.get("venue_id")
    target_date = body.get("target_date")
    preferred_time = body.get("preferred_time")

    if not user_id or not venue_id or not target_date or not preferred_time:
        raise HTTPException(400, "user_id, venue_id, target_date, and preferred_time are required")

    claim = await db.create_slot_claim(
        user_id=user_id,
        venue_id=venue_id,
        target_date=target_date,
        preferred_time=preferred_time,
        seating_type=body.get("seating_type", ""),
        window_minutes=body.get("window_minutes", 30),
        reservation_id=body.get("reservation_id"),
    )
    return {"created": True, "claim": claim}


@router.post("/vip/claims/override", dependencies=[Depends(_require_admin)])
async def override_claim(body: dict):
    """Admin override: cancel conflicting claims and create admin's priority claim.

    This allows the admin to jump the queue on any slot regardless of
    existing user claims.
    """
    venue_id = body.get("venue_id")
    target_date = body.get("target_date")
    preferred_time = body.get("preferred_time")
    admin_user_id = body.get("admin_user_id")

    if not venue_id or not target_date or not preferred_time or not admin_user_id:
        raise HTTPException(400, "venue_id, target_date, preferred_time, and admin_user_id are required")

    # Cancel all active claims for this exact slot
    claims = await db.get_active_claims(venue_id, target_date)
    overridden = []
    for c in claims:
        if c["preferred_time"] == preferred_time and c["status"] == "active":
            await db.update_slot_claim(c["id"], status="overridden")
            overridden.append(c["id"])

    # Create admin's priority claim
    claim = await db.create_slot_claim(
        user_id=admin_user_id,
        venue_id=venue_id,
        target_date=target_date,
        preferred_time=preferred_time,
        seating_type=body.get("seating_type", ""),
        venue_name=body.get("venue_name", ""),
    )

    return {
        "overridden_count": len(overridden),
        "overridden_ids": overridden,
        "admin_claim": claim,
    }


# ---------------------------------------------------------------------------
# Active Jobs Dashboard
# ---------------------------------------------------------------------------


@router.get("/vip/active-jobs", dependencies=[Depends(_require_admin)])
async def get_active_jobs(request: Request):
    """List all running jobs with their real-time status.

    Returns in-memory job states enriched with DB data (venue name,
    target date, mode, group_id, poll tier).
    """
    jobs = request.app.state.jobs.list_all_jobs()

    # Enrich each job with DB data
    enriched = []
    for j in jobs:
        try:
            row = await db.get_reservation_by_id(j["reservation_id"])
        except Exception:
            row = None

        if row:
            j["venue_name"] = row.get("venue_name", "")
            j["venue_id"] = row.get("venue_id")
            j["target_date"] = str(row.get("target_date", ""))
            j["mode"] = row.get("mode", "")
            j["group_id"] = row.get("group_id")
            j["created_at"] = str(row.get("created_at", ""))
            j["poll_tier"] = row.get("poll_tier", "warm")
            j["status"] = row.get("status", "")
        enriched.append(j)

    return {"jobs": enriched, "total": len(enriched)}


@router.post("/vip/cancel-job/{reservation_id}", dependencies=[Depends(_require_admin)])
async def admin_cancel_job(reservation_id: str, request: Request):
    """Cancel a specific job from the admin dashboard."""
    # Cancel in-memory job
    job_manager = request.app.state.jobs
    job = job_manager.get(reservation_id)
    if job:
        job.broadcast("snipe_result", {"success": False, "error": "Cancelled by admin"})
        job_manager.remove(reservation_id)

    # Cancel APScheduler jobs
    scheduler = request.app.state.scheduler
    if scheduler:
        for prefix in ("snipe_", "monitor_"):
            try:
                scheduler.remove_job(f"{prefix}{reservation_id}")
            except Exception:
                pass

    # Update DB
    await db.update_reservation(reservation_id, status="cancelled", error="Cancelled by admin")
    return {"cancelled": True}


# ---------------------------------------------------------------------------
# Cancellation Analytics
# ---------------------------------------------------------------------------


@router.get("/vip/analytics/cancellation-by-hour", dependencies=[Depends(_require_admin)])
async def analytics_cancellation_by_hour(venue_id: int | None = None):
    """Aggregate slot snapshots by hour of day to show cancellation patterns.

    Returns slot_count changes (cancellations = slots appearing) by hour.
    This tells us WHEN cancellations happen — the key competitive intel
    for optimizing polling schedules.
    """
    svc = db.get_service_client()
    query = svc.table("slot_snapshots").select("captured_at,slot_count,venue_id")
    if venue_id:
        query = query.eq("venue_id", venue_id)
    # Get snapshots where slots appeared (cancellation signal)
    query = query.gt("slot_count", 0).order("captured_at", desc=True).limit(2000)
    resp = await query.execute()
    snapshots = resp.data or []

    # Bucket by hour of day (ET)
    hours = {h: 0 for h in range(24)}
    for snap in snapshots:
        try:
            ts = snap.get("captured_at", "")
            # Parse ISO timestamp — captured_at is TIMESTAMPTZ
            from datetime import datetime as _dt
            if "T" in ts:
                # Handle both Z and +00:00 suffixes
                clean = ts.replace("Z", "+00:00")
                try:
                    dt = _dt.fromisoformat(clean)
                except ValueError:
                    continue
                # Convert to ET
                try:
                    from zoneinfo import ZoneInfo
                except ImportError:
                    from backports.zoneinfo import ZoneInfo
                dt_et = dt.astimezone(ZoneInfo("America/New_York"))
                hours[dt_et.hour] += 1
        except Exception:
            continue

    return {
        "hours": [{"hour": h, "count": c} for h, c in sorted(hours.items())],
        "total_snapshots": len(snapshots),
        "venue_id": venue_id,
    }


@router.get("/vip/analytics/cancellation-by-day", dependencies=[Depends(_require_admin)])
async def analytics_cancellation_by_day(venue_id: int | None = None):
    """Aggregate slot snapshots by day of week (0=Mon, 6=Sun).

    Shows which days have the most cancellations — useful for knowing
    when to poll more aggressively.
    """
    svc = db.get_service_client()
    query = svc.table("slot_snapshots").select("captured_at,slot_count,venue_id")
    if venue_id:
        query = query.eq("venue_id", venue_id)
    query = query.gt("slot_count", 0).order("captured_at", desc=True).limit(2000)
    resp = await query.execute()
    snapshots = resp.data or []

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    days = {i: 0 for i in range(7)}
    for snap in snapshots:
        try:
            ts = snap.get("captured_at", "")
            from datetime import datetime as _dt
            if "T" in ts:
                clean = ts.replace("Z", "+00:00")
                try:
                    dt = _dt.fromisoformat(clean)
                except ValueError:
                    continue
                try:
                    from zoneinfo import ZoneInfo
                except ImportError:
                    from backports.zoneinfo import ZoneInfo
                dt_et = dt.astimezone(ZoneInfo("America/New_York"))
                days[dt_et.weekday()] += 1
        except Exception:
            continue

    return {
        "days": [{"day": i, "name": day_names[i], "count": c} for i, c in sorted(days.items())],
        "total_snapshots": len(snapshots),
        "venue_id": venue_id,
    }


@router.get("/vip/analytics/slot-velocity", dependencies=[Depends(_require_admin)])
async def analytics_slot_velocity(venue_id: int, target_date: str):
    """Get slot count over time for a specific venue+date.

    Shows how quickly slots disappear after a drop or cancellation.
    Used for the slot velocity line chart.
    """
    snapshots = await db.get_slot_snapshots(venue_id, target_date, limit=500)
    return {
        "points": [
            {
                "time": s.get("captured_at", ""),
                "slot_count": s.get("slot_count", 0),
                "ms_since_drop": s.get("ms_since_drop"),
                "ms_since_start": s.get("ms_since_start"),
                "source": s.get("source", "unknown"),
            }
            for s in snapshots
        ],
        "total": len(snapshots),
    }


@router.get("/vip/analytics/booking-success", dependencies=[Depends(_require_admin)])
async def analytics_booking_success():
    """Aggregate booking outcomes across all reservations.

    Returns success vs failure breakdown, average attempts, and
    time-to-book stats — the core performance metrics.
    """
    svc = db.get_service_client()
    resp = await (
        svc.table("reservations")
        .select("status,attempts,elapsed_seconds,venue_name,mode,created_at,error")
        .in_("status", ["confirmed", "failed", "dry_run", "cancelled"])
        .order("created_at", desc=True)
        .limit(500)
        .execute()
    )
    reservations = resp.data or []

    confirmed = [r for r in reservations if r["status"] == "confirmed"]
    failed = [r for r in reservations if r["status"] == "failed"]
    dry_runs = [r for r in reservations if r["status"] == "dry_run"]
    cancelled = [r for r in reservations if r["status"] == "cancelled"]

    avg_attempts_success = (
        round(sum(r.get("attempts", 0) for r in confirmed) / len(confirmed), 1)
        if confirmed else 0
    )
    avg_time_success = (
        round(sum(r.get("elapsed_seconds", 0) for r in confirmed) / len(confirmed), 2)
        if confirmed else 0
    )

    # Common failure reasons
    error_counts: dict[str, int] = {}
    for r in failed:
        err = r.get("error", "Unknown")
        # Normalize similar errors
        if "Retry window" in (err or ""):
            err = "Retry window exhausted"
        elif "Max attempts" in (err or ""):
            err = "Max attempts reached"
        error_counts[err] = error_counts.get(err, 0) + 1

    top_errors = sorted(error_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "total": len(reservations),
        "confirmed": len(confirmed),
        "failed": len(failed),
        "dry_runs": len(dry_runs),
        "cancelled": len(cancelled),
        "success_rate": round(len(confirmed) / max(len(confirmed) + len(failed), 1) * 100, 1),
        "avg_attempts_success": avg_attempts_success,
        "avg_time_success_seconds": avg_time_success,
        "top_errors": [{"error": e, "count": c} for e, c in top_errors],
    }


# ---------------------------------------------------------------------------
# Restaurant Image Management
# ---------------------------------------------------------------------------


@router.get("/vip/restaurants")
async def list_restaurants_admin(token: str = Depends(_require_admin)):
    """List all curated restaurants for the admin restaurant manager."""
    rows = await db.list_curated_restaurants(active_only=False)
    return {
        "restaurants": [
            {
                "venue_id": r["venue_id"],
                "name": r["name"],
                "cuisine": r.get("cuisine", ""),
                "neighborhood": r.get("neighborhood", ""),
                "url_slug": r.get("url_slug", ""),
                "image_url": r.get("image_url"),
                "slot_interval": r.get("slot_interval", 15),
                "drop_days_ahead": r.get("drop_days_ahead"),
                "drop_hour": r.get("drop_hour"),
                "drop_minute": r.get("drop_minute", 0),
                "service_start": r.get("service_start", "17:00"),
                "service_end": r.get("service_end", "22:00"),
                "sort_order": r.get("sort_order", 0),
                "is_active": r.get("is_active", True),
            }
            for r in rows
        ]
    }


@router.patch("/vip/restaurants/{venue_id}/image")
async def update_restaurant_image(
    venue_id: int,
    body: dict,
    token: str = Depends(_require_admin),
):
    """Update a restaurant's image URL."""
    image_url = body.get("image_url", "").strip() or None
    updated = await db.update_restaurant_image(venue_id, image_url)
    if not updated:
        raise HTTPException(404, "Restaurant not found")
    return {"ok": True, "venue_id": venue_id, "image_url": image_url}


@router.patch("/vip/restaurants/{venue_id}")
async def update_restaurant(
    venue_id: int,
    body: dict,
    token: str = Depends(_require_admin),
):
    """Update any fields on a curated restaurant."""
    updated = await db.update_curated_restaurant(venue_id, body)
    if not updated:
        raise HTTPException(404, "Restaurant not found")
    return {"ok": True, "restaurant": updated}


@router.post("/vip/restaurants")
async def create_restaurant(
    body: dict,
    token: str = Depends(_require_admin),
):
    """Add a new curated restaurant."""
    venue_id = body.get("venue_id")
    if not venue_id:
        raise HTTPException(400, "venue_id is required")
    # Check if already exists
    existing = await db.get_curated_restaurant(int(venue_id))
    if existing:
        raise HTTPException(409, f"Restaurant with venue_id {venue_id} already exists")
    created = await db.create_curated_restaurant(body)
    return {"ok": True, "restaurant": created}


# ---------------------------------------------------------------------------
# Scout System — continuous restaurant monitoring
# ---------------------------------------------------------------------------


def _get_scout(request: Request):
    """Get the scout orchestrator from app state."""
    scout = getattr(request.app.state, "scout", None)
    if not scout:
        raise HTTPException(503, "Scout orchestrator not running")
    return scout


@router.get("/vip/scouts")
async def list_scouts(
    request: Request,
    token: str = Depends(_require_admin),
):
    """List all scout campaigns with live status."""
    scout = _get_scout(request)
    try:
        campaigns = await db.list_scout_campaigns(active_only=False)
    except Exception:
        campaigns = []

    result = []
    for c in campaigns:
        vid = c["venue_id"]
        ctype = c.get("type", "drop")
        live = scout.get_campaign_status(vid, ctype)
        entry = {
            **c,
            "running": live["running"] if live else False,
            "poll_count": live["poll_count"] if live else 0,
        }
        result.append(entry)

    return {"scouts": result, "active_count": scout.active_scouts}


@router.post("/vip/scouts/start")
async def start_scout(
    request: Request,
    body: dict,
    token: str = Depends(_require_admin),
):
    """Start a scout for a specific venue.

    Body params:
        venue_id (required)
        type: "drop" or "cancellation" (default: "drop")
        target_dates: ["2026-03-15", ...] (required for cancellation scouts)
        venue_name (optional — auto-detected from curated data)
    """
    scout = _get_scout(request)
    venue_id = body.get("venue_id")
    if not venue_id:
        raise HTTPException(400, "venue_id is required")

    campaign_type = body.get("type", "drop")
    if campaign_type not in ("drop", "cancellation"):
        raise HTTPException(400, "type must be 'drop' or 'cancellation'")

    venue_name = body.get("venue_name", "")
    curated = None

    if not venue_name:
        curated = await db.get_curated_restaurant(int(venue_id))
        if curated:
            venue_name = curated.get("name", "")

    config = {}

    if campaign_type == "drop":
        # Auto-detect drop time from curated data or body override
        if not curated:
            curated = await db.get_curated_restaurant(int(venue_id))
        drop_hour = body.get("drop_hour") or (curated.get("drop_hour") if curated else None) or 9
        drop_minute = body.get("drop_minute") or (curated.get("drop_minute") if curated else None) or 0
        config["drop_hour"] = drop_hour
        config["drop_minute"] = drop_minute

        # days_ahead from curated data (e.g. Semma = 15, not default 30)
        days_ahead = body.get("days_ahead") or (curated.get("drop_days_ahead") if curated else None) or 30
        config["days_ahead"] = int(days_ahead)

        # Optional explicit target_dates for drop scouts
        target_dates = body.get("target_dates", [])
        if target_dates:
            config["target_dates"] = target_dates

        # Auto-compute polling windows around drop time
        from tablement.web.scout import compute_drop_polling_windows
        config["polling_windows"] = compute_drop_polling_windows(drop_hour, drop_minute)

    elif campaign_type == "cancellation":
        target_dates = body.get("target_dates", [])
        if not target_dates:
            raise HTTPException(400, "target_dates is required for cancellation scouts")
        config["target_dates"] = target_dates
        # Default cancellation polling window: 10 AM - 10 PM
        config["polling_windows"] = [{"start": "10:00", "end": "22:00"}]

    campaign = await scout.start_scout(
        int(venue_id), venue_name, campaign_type=campaign_type, **config,
    )
    return {"ok": True, "campaign": campaign}


@router.post("/vip/scouts/stop")
async def stop_scout(
    request: Request,
    body: dict,
    token: str = Depends(_require_admin),
):
    """Stop a specific scout."""
    scout = _get_scout(request)
    venue_id = body.get("venue_id")
    if not venue_id:
        raise HTTPException(400, "venue_id is required")

    campaign_type = body.get("type")  # None = stop all types for this venue
    await scout.stop_scout(int(venue_id), campaign_type)
    return {"ok": True, "venue_id": venue_id, "type": campaign_type}


@router.post("/vip/scouts/stop-all")
async def stop_all_scouts(
    request: Request,
    token: str = Depends(_require_admin),
):
    """Stop all running scouts."""
    scout = _get_scout(request)
    count = await scout.stop_all()
    return {"ok": True, "stopped": count}


@router.patch("/vip/scouts/{venue_id}")
async def update_scout_config(
    venue_id: int,
    body: dict,
    request: Request,
    token: str = Depends(_require_admin),
):
    """Update scout campaign config. Only polling_windows is editable."""
    polling_windows = body.get("polling_windows")
    if polling_windows is None:
        raise HTTPException(400, "polling_windows is required")

    if not isinstance(polling_windows, list):
        raise HTTPException(400, "polling_windows must be a list")
    for w in polling_windows:
        if not isinstance(w, dict) or "start" not in w or "end" not in w:
            raise HTTPException(400, "Each window must have 'start' and 'end' (HH:MM format)")

    campaign_type = body.get("type", "drop")
    campaign = await db.update_scout_polling_windows(venue_id, campaign_type, polling_windows)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    return {"ok": True, "campaign": campaign}


@router.get("/vip/scouts/stats")
async def scout_stats(
    request: Request,
    token: str = Depends(_require_admin),
):
    """Get aggregate scout stats."""
    scout = _get_scout(request)
    try:
        db_stats = await db.get_scout_stats()
    except Exception:
        db_stats = {"total_campaigns": 0, "active_campaigns": 0, "total_polls": 0, "total_events": 0}
    return {
        **db_stats,
        "running_scouts": scout.active_scouts,
    }


@router.get("/vip/scouts/feed")
async def scout_feed(
    request: Request,
    token: str = Depends(_require_admin),
):
    """Recent events across all venues (live feed)."""
    try:
        events = await db.get_scout_events(limit=50)
    except Exception:
        events = []

    # Enrich with venue names — prefer DB column, fall back to in-memory campaigns
    scout = _get_scout(request)
    for evt in events:
        if evt.get("venue_name"):
            continue  # Already stored in DB (v3+)
        vid = evt.get("venue_id")
        venue_name = ""
        for key, campaign in scout._campaigns.items():
            if campaign.get("venue_id") == vid:
                venue_name = campaign.get("venue_name", "")
                break
        evt["venue_name"] = venue_name

    return {"events": events}


@router.get("/vip/scouts/{venue_id}/events")
async def scout_venue_events(
    venue_id: int,
    request: Request,
    token: str = Depends(_require_admin),
):
    """Get change events for a specific venue."""
    try:
        events = await db.get_scout_events(venue_id=venue_id, limit=100)
    except Exception:
        events = []
    return {"events": events}


@router.get("/vip/scouts/{venue_id}/snapshots")
async def scout_venue_snapshots(
    venue_id: int,
    request: Request,
    token: str = Depends(_require_admin),
):
    """Get availability snapshots for a venue."""
    try:
        snapshots = await db.get_scout_snapshots(venue_id, limit=100)
    except Exception:
        snapshots = []
    return {"snapshots": snapshots}


@router.get("/vip/scouts/heatmap")
async def scout_heatmap(
    request: Request,
    venue_id: int | None = None,
    token: str = Depends(_require_admin),
):
    """Get heatmap data: hour×day grid of slots_appeared events."""
    try:
        data = await db.get_scout_heatmap_data(venue_id)
    except Exception:
        data = []

    # Build full 24×7 grid with zeros
    grid = [[0] * 7 for _ in range(24)]
    for row in data:
        h = row["hour_et"]
        d = row["day_of_week"]
        if 0 <= h < 24 and 0 <= d < 7:
            grid[h][d] = row["event_count"]

    return {
        "grid": grid,
        "hours": list(range(24)),
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "venue_id": venue_id,
        "total_events": sum(sum(row) for row in grid),
    }


@router.get("/vip/scouts/{venue_id}/timeline")
async def scout_timeline(
    venue_id: int,
    request: Request,
    target_date: str | None = None,
    token: str = Depends(_require_admin),
):
    """Slot sighting timeline — when slots appeared/disappeared with durations."""
    try:
        sightings = await db.get_slot_timeline(venue_id, target_date, limit=100)
    except Exception:
        sightings = []
    return {"sightings": sightings, "venue_id": venue_id, "target_date": target_date}


@router.get("/vip/scouts/{venue_id}/sighting-stats")
async def scout_sighting_stats(
    venue_id: int,
    request: Request,
    token: str = Depends(_require_admin),
):
    """Aggregated sighting stats for a venue (avg duration, fastest disappearance, etc.)."""
    try:
        stats = await db.get_sighting_stats(venue_id)
    except Exception:
        stats = {"total_sightings": 0, "avg_duration_seconds": 0, "active_slots": 0}
    return stats


# ── Scout Errors ──────────────────────────────────────────────────────


@router.get("/vip/scouts/errors")
async def scout_errors(
    venue_id: int | None = None,
    token: str = Depends(_require_admin),
):
    """Get recent scout errors, optionally filtered by venue."""
    errors = await db.get_scout_errors(venue_id=venue_id, limit=100)
    return {"errors": errors}


@router.delete("/vip/scouts/errors")
async def clear_scout_errors(
    token: str = Depends(_require_admin),
):
    """Clear all scout errors."""
    await db.clear_scout_errors()
    return {"ok": True}


# ── Scout Settings ────────────────────────────────────────────────────


@router.get("/vip/scouts/settings")
async def get_scout_settings(
    token: str = Depends(_require_admin),
):
    """Get scout polling configuration."""
    settings = await db.get_scout_settings()
    return settings or {}


@router.patch("/vip/scouts/settings")
async def update_scout_settings(
    body: dict,
    token: str = Depends(_require_admin),
):
    """Update scout polling configuration (partial update)."""
    body.pop("id", None)
    body.pop("updated_at", None)
    settings = await db.update_scout_settings(**body)
    return {"ok": True, "settings": settings}
