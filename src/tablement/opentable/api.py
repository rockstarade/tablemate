"""Async OpenTable API client using httpx with HTTP/2.

Targets the mobile API (mobile-api.opentable.com) which has lighter
anti-bot than the web GraphQL surface (Akamai Bot Manager).

3-step booking flow:
  1. find_slots()       → PUT /api/v3/restaurant/availability  (get slot_hashes)
  2. lock_slot()        → POST /api/v1/reservation/{id}/lock   (temporary hold)
  3. complete_booking() → POST /api/v1/reservation/{id}        (finalize)

Anti-detection (3 layers — same strategy as Resy):
  Layer 1: Per-user residential proxy (shared proxy_pool)
  Layer 2: Behavioral — scout mode = no bearer token, booking mode = authenticated
  Layer 3: Mobile app fingerprints (MobileFingerprintPool)

Rate limits:
  - Mobile API: ~30s minimum between availability polls
  - 3 requests per time window, 1 concurrent (web; mobile is lighter)
  - Aggressive polling (<30s interval) risks IP ban
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
import time
from datetime import datetime

import httpx

from tablement.opentable.fingerprint import (
    MobileFingerprint,
    mobile_fingerprint_pool,
)
from tablement.opentable.models import (
    OTAuthToken,
    OTBookResponse,
    OTLockResponse,
    OTSlot,
)
from tablement.proxy import proxy_pool

logger = logging.getLogger(__name__)

_OT_API_HOST = "mobile-api.opentable.com"
_RESOLVED_IP: str | None = None

# OpenTable partner attribution (standard mobile app value)
_PARTNER_ID = "84"


def _resolve_ot_ip() -> str | None:
    """Pre-resolve mobile-api.opentable.com to cache the IP."""
    global _RESOLVED_IP
    if _RESOLVED_IP is None:
        try:
            _RESOLVED_IP = socket.gethostbyname(_OT_API_HOST)
            logger.info("Pre-resolved %s -> %s", _OT_API_HOST, _RESOLVED_IP)
        except socket.gaierror:
            logger.warning("Failed to pre-resolve %s", _OT_API_HOST)
    return _RESOLVED_IP


def _build_availability_token(premium: bool = False) -> str:
    """Build the base64-encoded availability token required by OT.

    Format: base64({"v":2,"m":1,"p":0,"s":0,"n":0})
    """
    payload = {"v": 2, "m": 1, "p": 1 if premium else 0, "s": 0, "n": 0}
    return base64.b64encode(json.dumps(payload).encode()).decode()


class OpenTableApiClient:
    """Async HTTP client for the OpenTable Mobile API.

    Use as an async context manager:

        async with OpenTableApiClient(auth_token=token) as client:
            slots = await client.find_slots(restaurant_id=994474, ...)

    Scout mode (monitoring without auth):

        async with OpenTableApiClient(scout=True) as client:
            slots = await client.find_slots(...)
    """

    BASE_URL = f"https://{_OT_API_HOST}"

    def __init__(
        self,
        auth_token: OTAuthToken | None = None,
        snipe_mode: bool = False,
        user_id: str | None = None,
        fingerprint: MobileFingerprint | None = None,
        scout: bool = False,
        local_address: str | None = None,
    ) -> None:
        self._auth_token = auth_token
        self._snipe_mode = snipe_mode
        self._user_id = user_id
        self._scout = scout
        self._local_address = local_address
        self._client: httpx.AsyncClient | None = None

        # Akamai session cookies (populated by prewarm_akamai)
        self._akamai_cookies: dict[str, str] = {}

        # Clock offset: positive = OT server ahead of us
        self._clock_offset_s: float = 0.0

        # Layer 3: Assign a consistent mobile fingerprint
        self._fingerprint = fingerprint or mobile_fingerprint_pool.get_fingerprint(
            user_id
        )

    async def __aenter__(self) -> OpenTableApiClient:
        _resolve_ot_ip()

        timeout = (
            httpx.Timeout(5.0, connect=3.0)
            if self._snipe_mode
            else httpx.Timeout(15.0, connect=8.0)
        )

        # Layer 1: Per-user residential proxy (shared with Resy)
        proxy_url = proxy_pool.get_proxy_url(self._user_id)

        client_kwargs: dict = {
            "base_url": self.BASE_URL,
            "headers": self._base_headers(),
            "timeout": timeout,
        }

        if proxy_url:
            client_kwargs["proxy"] = proxy_url
            client_kwargs["http2"] = True
            logger.debug(
                "OT: using proxy for user %s", (self._user_id or "anon")[:8]
            )
        else:
            transport_kwargs: dict = {"http2": True}
            if self._local_address:
                transport_kwargs["local_address"] = self._local_address
            client_kwargs["transport"] = httpx.AsyncHTTPTransport(**transport_kwargs)

        self._client = httpx.AsyncClient(**client_kwargs)
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.aclose()

    # -----------------------------------------------------------------
    # Headers
    # -----------------------------------------------------------------

    def _base_headers(self) -> dict[str, str]:
        """Build base headers mimicking the OT mobile app.

        Layer 3: Mobile fingerprint headers. No sec-ch-ua or sec-fetch-*
        (those are browser-only; mobile API doesn't expect them).
        """
        headers = self._fingerprint.base_headers()

        # Auth (skip in scout mode — appears as anonymous browsing)
        if self._auth_token and not self._scout:
            headers["Authorization"] = f"Bearer {self._auth_token.bearer_token}"

        return headers

    def set_auth_token(self, token: OTAuthToken) -> None:
        """Update auth token on live client (reuse TCP connection)."""
        self._auth_token = token
        self._scout = False
        if self._client:
            self._client.headers["Authorization"] = (
                f"Bearer {token.bearer_token}"
            )

    # -----------------------------------------------------------------
    # Step 1: Find available slots
    # -----------------------------------------------------------------

    async def find_slots(
        self,
        restaurant_id: int,
        day: str,
        party_size: int,
        *,
        fast: bool = False,
    ) -> list[OTSlot]:
        """Find available time slots for a restaurant + date.

        Calls PUT /api/v3/restaurant/availability.

        Args:
            restaurant_id: OpenTable restaurant ID (e.g. 994474 for Don Angie)
            day:           Date string "YYYY-MM-DD"
            party_size:    Number of guests
            fast:          If True, skip extra parsing (snipe hot path)

        Returns:
            List of OTSlot objects with slot_hash for locking.
        """
        assert self._client is not None, "Client not initialized — use async with"

        body = {
            "dateTime": f"{day}T19:00",
            "partySize": party_size,
            "rids": [str(restaurant_id)],
            "forceNextAvailable": "true",
            "includeNextAvailable": True,
            "availabilityToken": _build_availability_token(),
            "requestPremium": "true",
            "requestDateMessages": True,
            "requestTicket": "true",
            "allowPop": True,
            "includeOffers": True,
            "requestAttributeTables": "true",
            "attribution": {"partnerId": _PARTNER_ID},
        }

        resp = await self._client.put("/api/v3/restaurant/availability", json=body)
        resp.raise_for_status()
        data = resp.json()

        # Parse slots from the nested response structure
        slots: list[OTSlot] = []
        for restaurant in data.get("availability", {}).get("restaurants", []):
            for timeslot in restaurant.get("timeslots", []):
                slot_hash = timeslot.get("slotHash", "")
                date_time = timeslot.get("dateTime", "")
                slot_type = ""
                # Extract seating type from dining areas if present
                for area in timeslot.get("diningAreas", []):
                    slot_type = area.get("tableAttribute", slot_type)
                if slot_hash and date_time:
                    slots.append(
                        OTSlot(
                            slot_hash=slot_hash,
                            date_time=date_time,
                            slot_type=slot_type,
                            available=True,
                        )
                    )

        logger.info(
            "OT find_slots: restaurant=%d date=%s party=%d → %d slots",
            restaurant_id,
            day,
            party_size,
            len(slots),
        )
        return slots

    async def find_slots_throttled(
        self,
        restaurant_id: int,
        day: str,
        party_size: int,
        min_interval_s: float = 30.0,
        _last_poll: dict | None = None,
    ) -> list[OTSlot]:
        """Rate-limited version of find_slots for monitoring.

        Ensures at least ``min_interval_s`` seconds between polls to
        avoid triggering OT's rate limits (30s minimum).
        """
        if _last_poll is None:
            _last_poll = {}
        key = f"{restaurant_id}:{day}"
        now = time.monotonic()
        last = _last_poll.get(key, 0.0)
        wait = min_interval_s - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_poll[key] = time.monotonic()
        return await self.find_slots(restaurant_id, day, party_size)

    # -----------------------------------------------------------------
    # Step 2: Lock a slot
    # -----------------------------------------------------------------

    async def lock_slot(
        self,
        restaurant_id: int,
        slot_hash: str,
        party_size: int,
        date_time: str,
    ) -> OTLockResponse:
        """Lock (temporarily hold) a time slot.

        Calls POST /api/v1/reservation/{restaurant_id}/lock.

        The lock is short-lived (typically 5-10 minutes). You must call
        ``complete_booking()`` within this window to finalize.

        Args:
            restaurant_id: OT restaurant ID
            slot_hash:     From find_slots() → OTSlot.slot_hash
            party_size:    Number of guests
            date_time:     ISO datetime from the slot

        Returns:
            OTLockResponse with lock_id for the booking step.
        """
        assert self._client is not None

        body = {
            "partySize": party_size,
            "dateTime": date_time,
            "hash": slot_hash,
            "attribution": {"partnerId": _PARTNER_ID},
        }

        resp = await self._client.post(
            f"/api/v1/reservation/{restaurant_id}/lock", json=body
        )
        resp.raise_for_status()
        data = resp.json()

        lock_id = data.get("lockId", "")
        expires = data.get("expiresAt", "")

        logger.info(
            "OT lock_slot: restaurant=%d hash=%s… → lock_id=%s",
            restaurant_id,
            slot_hash[:12],
            lock_id[:12] if lock_id else "FAILED",
        )

        return OTLockResponse(lock_id=lock_id, expires_at=expires)

    # -----------------------------------------------------------------
    # Step 3: Complete the booking
    # -----------------------------------------------------------------

    async def complete_booking(
        self,
        restaurant_id: int,
        lock_id: str,
        party_size: int,
        date_time: str,
    ) -> OTBookResponse:
        """Finalize the reservation after locking.

        Calls POST /api/v1/reservation/{restaurant_id}.

        Args:
            restaurant_id: OT restaurant ID
            lock_id:       From lock_slot() → OTLockResponse.lock_id
            party_size:    Number of guests
            date_time:     ISO datetime

        Returns:
            OTBookResponse with confirmation_number.
        """
        assert self._client is not None
        assert self._auth_token is not None, "Auth token required for booking"

        body = {
            "lockId": lock_id,
            "dinerId": self._auth_token.diner_id,
            "gpid": self._auth_token.gpid,
            "phoneNumber": self._auth_token.phone,
            "partySize": party_size,
            "dateTime": date_time,
            "latitude": self._auth_token.latitude,
            "longitude": self._auth_token.longitude,
            "optInSms": False,
            "optInEmail": False,
            "optInDataSharing": False,
            "attribution": {"partnerId": _PARTNER_ID},
        }

        resp = await self._client.post(
            f"/api/v1/reservation/{restaurant_id}", json=body
        )
        resp.raise_for_status()
        data = resp.json()

        confirmation = data.get("confirmationNumber", "")

        logger.info(
            "OT complete_booking: restaurant=%d lock=%s… → confirmation=%s",
            restaurant_id,
            lock_id[:12],
            confirmation or "FAILED",
        )

        return OTBookResponse(
            confirmation_number=confirmation,
            reservation_id=data.get("reservationId", ""),
            restaurant_name=data.get("restaurantName", ""),
            date_time=date_time,
            party_size=party_size,
        )

    # -----------------------------------------------------------------
    # Convenience: book in one call (lock + complete)
    # -----------------------------------------------------------------

    async def book(
        self,
        restaurant_id: int,
        slot_hash: str,
        party_size: int,
        date_time: str,
    ) -> OTBookResponse:
        """Combined lock + complete in one call.

        This is the primary booking method for snipe mode. It locks the
        slot and immediately books it in sequence.

        Raises on failure at either step.
        """
        lock = await self.lock_slot(restaurant_id, slot_hash, party_size, date_time)
        if not lock.lock_id:
            from tablement.errors import OTLockError

            raise OTLockError(
                f"Failed to lock slot {slot_hash[:12]}… at {date_time}"
            )

        return await self.complete_booking(
            restaurant_id, lock.lock_id, party_size, date_time
        )

    # -----------------------------------------------------------------
    # Anti-detection: Akamai session warmup
    # -----------------------------------------------------------------

    async def prewarm_akamai(self) -> dict[str, str]:
        """Pre-warm Akamai Bot Manager session.

        Equivalent to Resy's prewarm_imperva(). Makes a lightweight
        request to establish the sensor cookies before the snipe window.

        Returns the captured Akamai cookies (_abck, bm_sz, etc.).
        """
        assert self._client is not None

        try:
            # Light request to trigger Akamai sensor
            resp = await self._client.get(
                "/api/v3/app/info",
                headers={"Accept": "application/json"},
            )
            # Capture cookies for subsequent requests
            for name, value in resp.cookies.items():
                self._akamai_cookies[name] = value
                self._client.cookies.set(name, value)

            logger.info(
                "OT prewarm_akamai: captured %d cookies", len(self._akamai_cookies)
            )
        except Exception as exc:
            logger.warning("OT prewarm_akamai failed: %s", exc)

        return self._akamai_cookies

    # -----------------------------------------------------------------
    # Clock calibration
    # -----------------------------------------------------------------

    async def calibrate_ot_clock(self, samples: int = 5) -> float:
        """Measure offset between our clock and OT's server clock.

        Sends lightweight HEAD/GET requests and compares the server
        Date header to our local time.

        Returns:
            Offset in seconds (positive = OT server ahead of us).
        """
        assert self._client is not None

        offsets: list[float] = []
        for _ in range(samples):
            t_before = time.time()
            try:
                resp = await self._client.head(f"{self.BASE_URL}/api/v3/app/info")
                t_after = time.time()
                rtt = t_after - t_before
                server_date = resp.headers.get("date", "")
                if server_date:
                    server_ts = datetime.strptime(
                        server_date, "%a, %d %b %Y %H:%M:%S %Z"
                    ).timestamp()
                    local_ts = t_before + (rtt / 2)
                    offsets.append(server_ts - local_ts)
            except Exception:
                pass
            await asyncio.sleep(0.1)

        if offsets:
            # Use median for robustness
            offsets.sort()
            self._clock_offset_s = offsets[len(offsets) // 2]
            logger.info("OT clock offset: %.3fs", self._clock_offset_s)

        return self._clock_offset_s

    # -----------------------------------------------------------------
    # Keep-alive ping
    # -----------------------------------------------------------------

    async def ping(self) -> bool:
        """Keep-alive ping to maintain TCP/TLS connection.

        Returns True if the connection is healthy.
        """
        assert self._client is not None
        try:
            resp = await self._client.head(f"{self.BASE_URL}/api/v3/app/info")
            return resp.status_code < 500
        except Exception:
            return False

    # -----------------------------------------------------------------
    # Latency measurement
    # -----------------------------------------------------------------

    async def measure_latency(self, samples: int = 3) -> dict[str, float]:
        """Measure round-trip latency to OT API.

        Returns dict with min, avg, max in milliseconds.
        """
        assert self._client is not None

        times: list[float] = []
        for _ in range(samples):
            t0 = time.monotonic()
            try:
                await self._client.head(f"{self.BASE_URL}/api/v3/app/info")
                times.append((time.monotonic() - t0) * 1000)
            except Exception:
                pass
            await asyncio.sleep(0.05)

        if not times:
            return {"min": 0, "avg": 0, "max": 0}

        return {
            "min": round(min(times), 1),
            "avg": round(sum(times) / len(times), 1),
            "max": round(max(times), 1),
        }

    # -----------------------------------------------------------------
    # Token validation
    # -----------------------------------------------------------------

    async def validate_token(self) -> dict:
        """Validate the bearer token by making a lightweight authenticated call.

        Returns user info if valid, raises on failure.
        """
        assert self._client is not None
        assert self._auth_token is not None

        resp = await self._client.get(
            "/api/v1/user/profile",
            headers={"Authorization": f"Bearer {self._auth_token.bearer_token}"},
        )
        resp.raise_for_status()
        data = resp.json()

        return {
            "diner_id": data.get("dinerId", ""),
            "gpid": data.get("gpid", ""),
            "first_name": data.get("firstName", ""),
            "last_name": data.get("lastName", ""),
            "email": data.get("email", ""),
            "phone": data.get("phoneNumber", ""),
        }
