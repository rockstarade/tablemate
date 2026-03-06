"""User notifications for booking confirmations.

Sends SMS and/or email when a reservation is secured.
Designed to never block or crash the booking flow — notification
failures are logged but silently ignored.

Channels:
    1. SMS via Twilio (toll-free number — no A2P 10DLC needed)
    2. Email via Resend (free tier: 100/day)

Configuration (.env):
    # Twilio (toll-free SMS)
    TWILIO_ACCOUNT_SID=ACxxxxxxx
    TWILIO_AUTH_TOKEN=xxxxxxx
    TWILIO_FROM_NUMBER=+18005551234   # your toll-free number

    # Resend (email)
    RESEND_API_KEY=re_xxxxxxx
    RESEND_FROM_EMAIL=bookings@yourdomain.com
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)


async def send_booking_confirmation(
    *,
    phone: str | None = None,
    email: str | None = None,
    venue_name: str,
    slot_time: str,
    target_date: str,
    party_size: int = 2,
    resy_token: str | None = None,
) -> dict:
    """Send booking confirmation via all available channels.

    Returns a dict summarizing what was sent and any errors.
    Never raises — booking flow must not be interrupted by notification failures.
    """
    result = {"sms_sent": False, "email_sent": False, "errors": []}

    # Format the message
    msg = _format_message(
        venue_name=venue_name,
        slot_time=slot_time,
        target_date=target_date,
        party_size=party_size,
        resy_token=resy_token,
    )

    # Try SMS
    if phone and _twilio_configured():
        try:
            await _send_sms(phone, msg)
            result["sms_sent"] = True
            logger.info("SMS confirmation sent to %s***%s", phone[:5], phone[-2:])
        except Exception as e:
            err = f"SMS failed: {e}"
            result["errors"].append(err)
            logger.warning(err)

    # Try email
    if email and _resend_configured():
        try:
            await _send_email(
                to=email,
                subject=f"Reservation Confirmed — {venue_name}",
                body=msg,
                venue_name=venue_name,
                slot_time=slot_time,
                target_date=target_date,
                party_size=party_size,
            )
            result["email_sent"] = True
            logger.info("Email confirmation sent to %s", email)
        except Exception as e:
            err = f"Email failed: {e}"
            result["errors"].append(err)
            logger.warning(err)

    if not result["sms_sent"] and not result["email_sent"]:
        if not phone and not email:
            logger.debug("No phone or email available — skipping notification")
        else:
            logger.warning(
                "No notification sent (phone=%s, email=%s, twilio=%s, resend=%s)",
                bool(phone), bool(email), _twilio_configured(), _resend_configured(),
            )

    return result


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _format_message(
    *,
    venue_name: str,
    slot_time: str,
    target_date: str,
    party_size: int,
    resy_token: str | None = None,
) -> str:
    """Format the confirmation SMS/email body."""
    lines = [
        f"Your reservation at {venue_name} has been secured!",
        "",
        f"Date: {target_date}",
        f"Time: {slot_time}",
        f"Party size: {party_size}",
    ]
    if resy_token:
        lines.append(f"Confirmation: {resy_token}")
    lines.append("")
    lines.append("— TablePass")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SMS via Twilio
# ---------------------------------------------------------------------------

def _twilio_configured() -> bool:
    return all([
        os.environ.get("TWILIO_ACCOUNT_SID"),
        os.environ.get("TWILIO_AUTH_TOKEN"),
        os.environ.get("TWILIO_FROM_NUMBER"),
    ])


async def _send_sms(to: str, body: str) -> None:
    """Send SMS via Twilio REST API (async, no SDK dependency)."""
    import httpx

    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            auth=(account_sid, auth_token),
            data={
                "To": to,
                "From": from_number,
                "Body": body,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        logger.debug("Twilio response: sid=%s, status=%s", data.get("sid"), data.get("status"))


# ---------------------------------------------------------------------------
# Email via Resend
# ---------------------------------------------------------------------------

def _resend_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


async def _send_email(
    *,
    to: str,
    subject: str,
    body: str,
    venue_name: str,
    slot_time: str,
    target_date: str,
    party_size: int,
) -> None:
    """Send email via Resend API (async, no SDK dependency)."""
    import httpx

    api_key = os.environ["RESEND_API_KEY"]
    from_email = os.environ.get("RESEND_FROM_EMAIL", "TablePass <onboarding@resend.dev>")

    # Build a simple HTML email
    html = _build_email_html(
        venue_name=venue_name,
        slot_time=slot_time,
        target_date=target_date,
        party_size=party_size,
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_email,
                "to": [to],
                "subject": subject,
                "html": html,
                "text": body,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        logger.debug("Resend response: id=%s", data.get("id"))


def _build_email_html(
    *,
    venue_name: str,
    slot_time: str,
    target_date: str,
    party_size: int,
) -> str:
    """Build a clean HTML email for the booking confirmation."""
    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 480px; margin: 0 auto; padding: 40px 20px; color: #1a1a1a;">
  <div style="text-align: center; margin-bottom: 32px;">
    <h1 style="font-size: 20px; font-weight: 700; margin: 0; font-family: 'Georgia', serif;">TablePass</h1>
  </div>

  <div style="background: #f8f8f8; border-radius: 16px; padding: 32px; text-align: center;">
    <div style="font-size: 32px; margin-bottom: 8px;">&#127869;</div>
    <h2 style="font-size: 18px; font-weight: 700; margin: 0 0 4px;">Reservation Confirmed</h2>
    <p style="color: #666; font-size: 14px; margin: 0;">Your table has been secured</p>
  </div>

  <div style="margin-top: 24px; padding: 24px; border: 1px solid #eee; border-radius: 12px;">
    <table style="width: 100%; font-size: 14px; border-collapse: collapse;">
      <tr>
        <td style="padding: 8px 0; color: #888;">Restaurant</td>
        <td style="padding: 8px 0; text-align: right; font-weight: 600;">{venue_name}</td>
      </tr>
      <tr>
        <td style="padding: 8px 0; color: #888;">Date</td>
        <td style="padding: 8px 0; text-align: right; font-weight: 600;">{target_date}</td>
      </tr>
      <tr>
        <td style="padding: 8px 0; color: #888;">Time</td>
        <td style="padding: 8px 0; text-align: right; font-weight: 600;">{slot_time}</td>
      </tr>
      <tr>
        <td style="padding: 8px 0; color: #888;">Party Size</td>
        <td style="padding: 8px 0; text-align: right; font-weight: 600;">{party_size}</td>
      </tr>
    </table>
  </div>

  <p style="text-align: center; color: #999; font-size: 12px; margin-top: 32px;">
    TablePass &mdash; NYC Restaurant Reservations
  </p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Lookup user contact info from Supabase
# ---------------------------------------------------------------------------

async def get_user_contact(user_id: str) -> dict:
    """Get a user's phone and email from Supabase auth.

    Returns {"phone": str|None, "email": str|None}.
    """
    try:
        from tablement.web import db
        service = db.get_service_client()

        # Query auth.users via the admin API
        resp = await service.auth.admin.get_user_by_id(user_id)
        if resp and resp.user:
            return {
                "phone": resp.user.phone or None,
                "email": resp.user.email or None,
            }
    except Exception as e:
        logger.warning("Failed to look up contact for user %s: %s", user_id[:8], e)

    return {"phone": None, "email": None}
