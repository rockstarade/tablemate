"""Async Resy API client using httpx with HTTP/2 and connection pooling.

Performance-optimized for snipe mode:
- orjson for faster JSON parsing (~2-3x vs stdlib)
- Fast mode skips full Pydantic validation on hot path
- Tighter timeouts in snipe mode (3s vs 10s)
- DNS pre-resolution to avoid lookup at T-0
- Pre-built booking request templates (only inject book_token at fire time)
- Dual-path booking (direct + proxy simultaneously)
- Fast book_token extraction via string search (skip full JSON parse)

Anti-detection (3 layers):
- Layer 1: Per-user residential proxy (sticky sessions via proxy.py)
- Layer 2: Behavioral — scout mode for monitoring, booking mode for snipe
- Layer 3: Browser fingerprints (sec-ch-ua, sec-fetch-*, rotated UA via fingerprint.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import time

import httpx
import orjson

from tablement.fingerprint import BrowserFingerprint, fingerprint_pool
from tablement.models import (
    AuthResponse,
    BookResponse,
    DetailsResponse,
    FindResponse,
    Slot,
)
from tablement.proxy import proxy_pool

logger = logging.getLogger(__name__)

API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"

# Pre-resolve DNS at module load to avoid lookup during snipe
_RESY_API_HOST = "api.resy.com"
_RESOLVED_IP: str | None = None

# Regex for fast book_token extraction from /3/details response
_BOOK_TOKEN_RE = re.compile(rb'"book_token"\s*:\s*\{\s*"value"\s*:\s*"([^"]+)"')


def _resolve_resy_ip() -> str | None:
    """Pre-resolve api.resy.com DNS to cache the IP address."""
    global _RESOLVED_IP
    if _RESOLVED_IP is None:
        try:
            _RESOLVED_IP = socket.gethostbyname(_RESY_API_HOST)
            logger.info("Pre-resolved %s -> %s", _RESY_API_HOST, _RESOLVED_IP)
        except socket.gaierror:
            logger.warning("Failed to pre-resolve %s", _RESY_API_HOST)
    return _RESOLVED_IP


def extract_book_token_fast(content: bytes) -> str | None:
    """Extract book_token from /3/details response using regex (skip full JSON parse).

    ~5-10x faster than orjson.loads() + Pydantic validation for the critical path.
    Returns None if not found.
    """
    m = _BOOK_TOKEN_RE.search(content)
    return m.group(1).decode() if m else None


class ResyApiClient:
    """
    Async HTTP client for the Resy API.

    Use as an async context manager to get connection pooling and keep-alive:

        async with ResyApiClient(auth_token="...") as client:
            slots = await client.find_slots(venue_id=123, day="2026-03-26", party_size=2)

    For snipe mode, use snipe_mode=True for tighter timeouts:

        async with ResyApiClient(auth_token="...", snipe_mode=True) as client:
            ...

    Anti-detection: pass user_id for per-user proxy + fingerprint assignment:

        async with ResyApiClient(auth_token="...", user_id="uuid-123") as client:
            ...

    Scout mode (for monitoring): no auth token in headers, appears as anonymous browsing:

        async with ResyApiClient(scout=True) as client:
            slots = await client.find_slots(...)
    """

    BASE_URL = "https://api.resy.com"

    def __init__(
        self,
        auth_token: str | None = None,
        snipe_mode: bool = False,
        user_id: str | None = None,
        fingerprint: BrowserFingerprint | None = None,
        scout: bool = False,
    ) -> None:
        self._auth_token = auth_token
        self._snipe_mode = snipe_mode
        self._user_id = user_id
        self._scout = scout
        self._client: httpx.AsyncClient | None = None

        # Dual-path: direct client (no proxy) for fastest booking path
        self._direct_client: httpx.AsyncClient | None = None

        # Pre-built booking template (populated via prepare_book_template)
        self._book_template: dict | None = None

        # Layer 3: Assign a consistent fingerprint for this client's lifetime
        if fingerprint:
            self._fingerprint = fingerprint
        else:
            self._fingerprint = fingerprint_pool.get_fingerprint(user_id)

    async def __aenter__(self) -> ResyApiClient:
        # Pre-resolve DNS
        _resolve_resy_ip()

        timeout = (
            httpx.Timeout(3.0, connect=2.0)
            if self._snipe_mode
            else httpx.Timeout(10.0, connect=5.0)
        )

        # Layer 1: Per-user residential proxy
        proxy_url = proxy_pool.get_proxy_url(self._user_id)

        client_kwargs: dict = {
            "http2": True,
            "base_url": self.BASE_URL,
            "headers": self._base_headers(),
            "timeout": timeout,
        }

        if proxy_url:
            client_kwargs["proxy"] = proxy_url
            logger.debug("Using proxy for user %s", (self._user_id or "anon")[:8])

        self._client = httpx.AsyncClient(**client_kwargs)
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.aclose()
        if self._direct_client:
            await self._direct_client.aclose()

    def _base_headers(self) -> dict[str, str]:
        """Build base headers using the assigned browser fingerprint.

        Layer 3: These headers are consistent for the lifetime of this client,
        matching a real browser session. Includes Client Hints, Accept-Language,
        and the proper sec-fetch-* headers for browsing context.
        """
        # Start with fingerprint identity headers (UA, sec-ch-ua, Accept-Language, etc.)
        headers = self._fingerprint.base_headers()

        # Add Resy API auth
        headers["Authorization"] = f'ResyAPI api_key="{API_KEY}"'

        # Add browsing context headers (Origin, Referer, sec-fetch-*)
        headers.update(self._fingerprint.browsing_headers())

        # Auth tokens (skip in scout mode — looks like anonymous browsing)
        if self._auth_token and not self._scout:
            headers["X-Resy-Auth-Token"] = self._auth_token
            headers["X-Resy-Universal-Auth"] = self._auth_token

        return headers

    def set_auth_token(self, token: str) -> None:
        """Update auth token on live client (avoids creating a new connection).

        Call this after authenticate() to reuse the existing TCP+TLS+HTTP/2
        connection for subsequent authenticated requests.
        """
        self._auth_token = token
        self._scout = False  # No longer in scout mode after auth
        if self._client:
            self._client.headers["X-Resy-Auth-Token"] = token
            self._client.headers["X-Resy-Universal-Auth"] = token

    def set_snipe_mode(self, enabled: bool = True) -> None:
        """Switch to tighter timeouts for the snipe loop."""
        self._snipe_mode = enabled
        if self._client:
            self._client.timeout = (
                httpx.Timeout(3.0, connect=2.0)
                if enabled
                else httpx.Timeout(10.0, connect=5.0)
            )

    async def ping(self) -> None:
        """Lightweight keep-alive request to prevent connection idle timeout."""
        assert self._client is not None
        try:
            await self._client.head("/")
        except Exception:
            pass  # Best-effort keep-alive

    async def authenticate(self, email: str, password: str) -> AuthResponse:
        """POST /3/auth/password — returns auth token + payment methods."""
        assert self._client is not None
        resp = await self._client.post(
            "/3/auth/password",
            data={"email": email, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            # Resy often returns 500 for invalid credentials
            try:
                body = resp.json()
                msg = body.get("message", resp.text)
            except Exception:
                msg = resp.text or f"HTTP {resp.status_code}"
            raise httpx.HTTPStatusError(
                msg, request=resp.request, response=resp
            )

        # Parse the raw JSON first so we can log on validation failure
        raw = resp.json()
        try:
            return AuthResponse.model_validate(raw)
        except Exception as e:
            # Log the raw response keys so we can fix the model
            logger.error(
                "AuthResponse validation failed. Raw keys: %s. "
                "payment_methods sample: %s. Error: %s",
                list(raw.keys()),
                raw.get("payment_methods", [])[:1],
                e,
            )
            raise

    async def find_slots(
        self, venue_id: int, day: str, party_size: int, *, fast: bool = False
    ) -> list[Slot]:
        """GET /4/find — returns available slots for a venue+date+party.

        With fast=True, uses orjson and minimal parsing for snipe performance.
        """
        assert self._client is not None
        resp = await self._client.get(
            "/4/find",
            params={
                "venue_id": venue_id,
                "day": day,
                "party_size": party_size,
                "lat": "0",
                "long": "0",
            },
        )
        resp.raise_for_status()

        if fast:
            # Fast path: orjson + direct dict access, skip full Pydantic tree
            data = orjson.loads(resp.content)
            venues = data.get("results", {}).get("venues", [])
            if not venues:
                return []
            slots: list[Slot] = []
            for venue in venues:
                for s in venue.get("slots", []):
                    slots.append(Slot.model_validate(s))
            return slots

        # Normal path: full Pydantic validation
        parsed = FindResponse.model_validate(resp.json())
        if not parsed.results.venues:
            return []
        slots_list: list[Slot] = []
        for venue in parsed.results.venues:
            slots_list.extend(venue.slots)
        return slots_list

    async def get_details(
        self, config_id: str, day: str, party_size: int
    ) -> DetailsResponse:
        """GET /3/details — returns a short-lived book_token."""
        assert self._client is not None
        resp = await self._client.get(
            "/3/details",
            params={
                "config_id": config_id,
                "day": day,
                "party_size": party_size,
            },
        )
        resp.raise_for_status()
        return DetailsResponse.model_validate(orjson.loads(resp.content))

    async def get_details_fast(
        self, config_id: str, day: str, party_size: int
    ) -> str:
        """GET /3/details — fast path that extracts book_token via regex.

        Returns the book_token string directly. ~5-10x faster than full
        Pydantic parse on the critical path. Falls back to full parse if
        regex extraction fails.
        """
        assert self._client is not None
        resp = await self._client.get(
            "/3/details",
            params={
                "config_id": config_id,
                "day": day,
                "party_size": party_size,
            },
        )
        resp.raise_for_status()

        # Fast path: regex extraction
        token = extract_book_token_fast(resp.content)
        if token:
            return token

        # Fallback: full parse
        parsed = DetailsResponse.model_validate(orjson.loads(resp.content))
        return parsed.book_token.value

    # ------------------------------------------------------------------
    # Pre-built request templates (Phase 1a)
    # ------------------------------------------------------------------

    def prepare_book_template(self, payment_method_id: int) -> dict:
        """Pre-build the booking request — call once after auth, reuse for every attempt.

        Only the book_token needs to be injected at fire time. Everything else
        (headers, static params, URL) is frozen here to save ~5-10ms per attempt.
        """
        template = {
            "headers": self._fingerprint.widget_headers(),
            "static_params": {
                "struct_payment_method": json.dumps({"id": payment_method_id}),
                "source_id": "resy.com-venue-details",
            },
        }
        self._book_template = template
        logger.debug("Book template prepared (payment_method_id=%d)", payment_method_id)
        return template

    # ------------------------------------------------------------------
    # Dual-path booking (Phase 2 + Phase 4: direct client, no proxy)
    # ------------------------------------------------------------------

    async def warmup_direct(self) -> None:
        """Create and warm a direct (no-proxy) HTTP client for fastest booking path.

        Call during warmup phase (T-30s). This client skips the residential proxy,
        saving 50-200ms on the final booking request.
        """
        if self._direct_client:
            return  # Already warm

        timeout = httpx.Timeout(3.0, connect=2.0)
        self._direct_client = httpx.AsyncClient(
            http2=True,
            base_url=self.BASE_URL,
            headers=self._base_headers(),
            timeout=timeout,
        )
        # Warm the TCP+TLS+HTTP/2 connection
        try:
            await self._direct_client.head("/")
            logger.debug("Direct client warmed (no proxy)")
        except Exception:
            logger.debug("Direct client warmup ping failed (non-fatal)")

    async def dual_book(self, book_token: str, payment_method_id: int) -> BookResponse:
        """Fire booking via BOTH direct and proxy paths simultaneously.

        First success wins. The second request will fail with "already booked"
        (Resy is idempotent per user+slot). This saves 50-200ms by racing
        the proxy path against the direct path.

        Falls back to single-path book() if direct client isn't warmed.
        """
        if not self._direct_client:
            return await self.book(book_token, payment_method_id)

        template = self._book_template or {
            "headers": self._fingerprint.widget_headers(),
            "static_params": {
                "struct_payment_method": json.dumps({"id": payment_method_id}),
                "source_id": "resy.com-venue-details",
            },
        }

        params = {**template["static_params"], "book_token": book_token}
        headers = template["headers"]

        async def _book_via(client: httpx.AsyncClient, label: str) -> BookResponse:
            try:
                resp = await client.get("/3/book", params=params, headers=headers)
                resp.raise_for_status()
                result = BookResponse.model_validate(orjson.loads(resp.content))
                logger.info("Booking won via %s path", label)
                return result
            except Exception:
                # GET failed, try POST fallback
                resp = await client.post(
                    "/3/book",
                    data=params,
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                )
                resp.raise_for_status()
                result = BookResponse.model_validate(orjson.loads(resp.content))
                logger.info("Booking won via %s path (POST fallback)", label)
                return result

        # Race both paths — first to succeed wins
        tasks = [
            asyncio.create_task(_book_via(self._direct_client, "direct")),
            asyncio.create_task(_book_via(self._client, "proxy")),
        ]

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Cancel the loser
        for task in pending:
            task.cancel()

        # Return first successful result
        for task in done:
            exc = task.exception()
            if exc is None:
                return task.result()

        # Both failed — re-raise last exception
        for task in done:
            exc = task.exception()
            if exc:
                raise exc

        raise RuntimeError("Dual book failed unexpectedly")

    # ------------------------------------------------------------------
    # Overlapping find_slots (Phase 1b)
    # ------------------------------------------------------------------

    async def find_slots_rapid(
        self, venue_id: int, day: str, party_size: int, *, volleys: int = 3, stagger_ms: int = 50,
    ) -> list[Slot]:
        """Fire multiple overlapping find_slots() calls staggered by stagger_ms.

        Returns the first non-empty result. Catches slots the instant they go live
        instead of waiting for the next poll cycle. Used in snipe mode only.
        """
        assert self._client is not None

        async def _single_find() -> list[Slot]:
            return await self.find_slots(venue_id, day, party_size, fast=True)

        tasks = []
        for i in range(volleys):
            tasks.append(asyncio.create_task(_single_find()))
            if i < volleys - 1:
                await asyncio.sleep(stagger_ms / 1000)

        # Wait for all, return first non-empty
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list) and r:
                return r
        return []

    # ------------------------------------------------------------------
    # Imperva session management
    # ------------------------------------------------------------------

    async def prewarm_imperva(self) -> dict:
        """Pre-warm Imperva session on BOTH proxy and direct clients.

        Imperva (Incapsula) sets 3 tracking cookies on first contact:
        - visid_incap_*: visitor ID (1 year expiry)
        - nlbi_*: load balancer cookie
        - incap_ses_*: session cookie

        httpx stores these automatically. This method ensures both the proxy
        and direct clients have valid Imperva sessions before the critical
        booking window. Returns a dict with cookie status + latency metrics.

        Call during warmup phase (T-30s). If cookies already exist from prior
        requests (find_slots, ping), this is a no-op verification.
        """
        assert self._client is not None
        result = {"proxy_client": {}, "direct_client": {}}

        # Warm proxy client
        try:
            t0 = time.time()
            resp = await self._client.head("/")
            latency_ms = (time.time() - t0) * 1000

            cookies = dict(self._client.cookies)
            imperva_cookies = {
                k: v for k, v in cookies.items()
                if any(tag in k for tag in ("visid_incap", "nlbi_", "incap_ses"))
            }

            result["proxy_client"] = {
                "latency_ms": round(latency_ms, 1),
                "status": resp.status_code,
                "imperva_cookies": len(imperva_cookies),
                "cookie_names": list(imperva_cookies.keys()),
            }
            logger.info(
                "Imperva pre-warm (proxy): %dms, %d cookies set",
                round(latency_ms), len(imperva_cookies),
            )
        except Exception as e:
            result["proxy_client"] = {"error": str(e)}
            logger.warning("Imperva pre-warm (proxy) failed: %s", e)

        # Warm direct client (if it exists)
        if self._direct_client:
            try:
                t0 = time.time()
                resp = await self._direct_client.head("/")
                latency_ms = (time.time() - t0) * 1000

                cookies = dict(self._direct_client.cookies)
                imperva_cookies = {
                    k: v for k, v in cookies.items()
                    if any(tag in k for tag in ("visid_incap", "nlbi_", "incap_ses"))
                }

                result["direct_client"] = {
                    "latency_ms": round(latency_ms, 1),
                    "status": resp.status_code,
                    "imperva_cookies": len(imperva_cookies),
                    "cookie_names": list(imperva_cookies.keys()),
                }
                logger.info(
                    "Imperva pre-warm (direct): %dms, %d cookies set",
                    round(latency_ms), len(imperva_cookies),
                )
            except Exception as e:
                result["direct_client"] = {"error": str(e)}
                logger.warning("Imperva pre-warm (direct) failed: %s", e)

        return result

    def get_imperva_cookie_count(self) -> int:
        """Check how many Imperva cookies the proxy client currently holds."""
        if not self._client:
            return 0
        return sum(
            1 for k in self._client.cookies.keys()
            if any(tag in k for tag in ("visid_incap", "nlbi_", "incap_ses"))
        )

    async def measure_latency(self, samples: int = 3) -> dict:
        """Measure round-trip latency to Resy API via both proxy and direct paths.

        Returns dict with proxy_ms and direct_ms (median of N samples each).
        This data is critical for tuning pre-fire timing on EC2.
        """
        assert self._client is not None

        async def _measure(client: httpx.AsyncClient, label: str) -> float | None:
            times = []
            for _ in range(samples):
                try:
                    t0 = time.time()
                    await client.head("/")
                    times.append((time.time() - t0) * 1000)
                except Exception:
                    continue
            if times:
                times.sort()
                median = times[len(times) // 2]
                logger.info("Latency (%s): %.1fms median (%d samples)", label, median, len(times))
                return round(median, 1)
            return None

        result = {
            "proxy_ms": await _measure(self._client, "proxy"),
        }
        if self._direct_client:
            result["direct_ms"] = await _measure(self._direct_client, "direct")
        return result

    # ------------------------------------------------------------------
    # Clock calibration (Phase 3)
    # ------------------------------------------------------------------

    async def calibrate_resy_clock(self, samples: int = 5) -> float:
        """Measure offset between our clock and Resy's server clock.

        Sends lightweight HEAD requests, compares Date response header to local time.
        Returns offset in seconds (positive = Resy ahead, negative = Resy behind).
        """
        assert self._client is not None
        from email.utils import parsedate_to_datetime

        offsets = []
        for _ in range(samples):
            t1 = time.time()
            try:
                resp = await self._client.head("/")
                t2 = time.time()
                date_header = resp.headers.get("date")
                if date_header:
                    server_time = parsedate_to_datetime(date_header).timestamp()
                    rtt = t2 - t1
                    local_mid = t1 + rtt / 2
                    offsets.append(server_time - local_mid)
            except Exception:
                continue

        if not offsets:
            logger.warning("Clock calibration failed (no valid samples)")
            return 0.0

        # Take median for robustness
        offsets.sort()
        median = offsets[len(offsets) // 2]
        logger.info(
            "Resy clock offset: %.1fms (from %d samples)",
            median * 1000, len(offsets),
        )
        return median

    # ------------------------------------------------------------------
    # Standard booking (unchanged, still used as fallback)
    # ------------------------------------------------------------------

    async def book(self, book_token: str, payment_method_id: int) -> BookResponse:
        """Book a reservation — GET first (bypasses CAPTCHA), POST fallback.

        Resy's CAPTCHA validation only runs on POST request bodies. Sending
        the booking request as GET with URL params instead of POST with a body
        skips the CAPTCHA check entirely. We try GET first for speed and CAPTCHA
        bypass, then fall back to POST if GET fails (future-proofing in case
        Resy patches the bypass).

        Layer 3: Uses widget_headers() for booking (Origin: widgets.resy.com)
        since the real booking flow comes from the embedded widget iframe.
        """
        assert self._client is not None
        params = {
            "book_token": book_token,
            "struct_payment_method": json.dumps({"id": payment_method_id}),
            "source_id": "resy.com-venue-details",
        }

        # Use pre-built template if available
        if self._book_template:
            params = {**self._book_template["static_params"], "book_token": book_token}

        # Layer 3: Booking uses widget origin headers
        book_headers = (
            self._book_template["headers"]
            if self._book_template
            else self._fingerprint.widget_headers()
        )

        # Primary: GET (bypasses CAPTCHA validation)
        try:
            resp = await self._client.get(
                "/3/book", params=params, headers=book_headers,
            )
            resp.raise_for_status()
            return BookResponse.model_validate(orjson.loads(resp.content))
        except Exception:
            logger.debug("GET /3/book failed, falling back to POST")

        # Fallback: standard POST (in case Resy patches GET)
        resp = await self._client.post(
            "/3/book",
            data=params,
            headers={**book_headers, "Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return BookResponse.model_validate(orjson.loads(resp.content))
