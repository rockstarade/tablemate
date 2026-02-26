"""Residential proxy pool with per-user sticky sessions.

Layer 1 of the anti-detection system. Routes each user's traffic through a
unique residential IP address so that Resy's WAF sees hundreds of different
IPs instead of one server IP.

Supported providers (all use the same session-ID pattern):
- Bright Data:    http://USER-zone-residential-session-{SID}:PASS@brd.superproxy.io:22225
- IPRoyal:        http://USER:PASS_session-{SID}_lifetime-30m@geo.iproyal.com:12321
- Oxylabs:        http://USER-session-{SID}:PASS@pr.oxylabs.io:7777
- SmartProxy:     http://USER-session-{SID}:PASS@gate.smartproxy.com:7000

Configuration via environment variables:
    PROXY_MODE       = "none" | "single" | "pool"     (default: "none")
    PROXY_URL        = base proxy URL with {session} placeholder
    PROXY_PROVIDER   = "brightdata" | "iproyal" | "oxylabs" | "smartproxy" | "generic"

Example .env for Bright Data:
    PROXY_MODE=pool
    PROXY_PROVIDER=brightdata
    PROXY_URL=http://brd-customer-XXX-zone-residential-session-{session}:PASSWORD@brd.superproxy.io:22225

Example .env for IPRoyal:
    PROXY_MODE=pool
    PROXY_PROVIDER=iproyal
    PROXY_URL=http://USERNAME:PASSWORD_session-{session}_lifetime-30m@geo.iproyal.com:12321

The {session} placeholder is replaced with a unique ID per user, giving each
user a sticky residential IP that persists for the session lifetime.

Proxy type switching (Admin VIP):
    PROXY_TYPE       = "residential" | "dedicated" | "direct"  (default: "residential")
    DEDICATED_PROXY_URL = URL for datacenter/dedicated proxy (no {session} needed)
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from enum import Enum

logger = logging.getLogger(__name__)


class ProxyType(str, Enum):
    """Which proxy infrastructure to route through."""
    RESIDENTIAL = "residential"
    DEDICATED = "dedicated"
    DIRECT = "direct"


# ---------------------------------------------------------------------------
# Proxy session management
# ---------------------------------------------------------------------------


class ProxyPool:
    """Manages proxy assignment for users.

    Each user gets a sticky session ID that maps to a consistent residential IP
    from the proxy provider. Session IDs rotate periodically to avoid long-term
    IP fingerprinting.

    Usage:
        pool = ProxyPool.from_env()
        proxy_url = pool.get_proxy_url(user_id="user-123")
        # → "http://...session-a1b2c3d4...@brd.superproxy.io:22225"
        # Pass this to httpx.AsyncClient(proxy=proxy_url)
    """

    def __init__(
        self,
        mode: str = "none",
        url_template: str = "",
        provider: str = "generic",
        session_lifetime_minutes: int = 30,
        proxy_type: ProxyType = ProxyType.RESIDENTIAL,
        dedicated_url: str = "",
    ) -> None:
        self.mode = mode  # "none", "single", "pool"
        self.url_template = url_template
        self.provider = provider
        self.session_lifetime_minutes = session_lifetime_minutes
        self.proxy_type = proxy_type
        self.dedicated_url = dedicated_url

        # user_id → (session_id, created_at)
        self._sessions: dict[str, tuple[str, float]] = {}

    @classmethod
    def from_env(cls) -> ProxyPool:
        """Create ProxyPool from environment variables."""
        mode = os.environ.get("PROXY_MODE", "none").lower()
        url = os.environ.get("PROXY_URL", "")
        provider = os.environ.get("PROXY_PROVIDER", "generic").lower()
        lifetime = int(os.environ.get("PROXY_SESSION_LIFETIME_MINUTES", "30"))
        dedicated_url = os.environ.get("DEDICATED_PROXY_URL", "")

        # Parse proxy type
        raw_type = os.environ.get("PROXY_TYPE", "residential").lower()
        try:
            proxy_type = ProxyType(raw_type)
        except ValueError:
            proxy_type = ProxyType.RESIDENTIAL

        if mode != "none" and not url:
            logger.warning("PROXY_MODE=%s but PROXY_URL not set — falling back to no proxy", mode)
            mode = "none"

        if mode != "none" and "{session}" not in url:
            logger.warning(
                "PROXY_URL missing {session} placeholder — all users will share one IP. "
                "Add {session} to your proxy URL for per-user sticky IPs."
            )

        pool = cls(
            mode=mode, url_template=url, provider=provider,
            session_lifetime_minutes=lifetime,
            proxy_type=proxy_type, dedicated_url=dedicated_url,
        )
        if mode != "none":
            logger.info(
                "Proxy pool initialized: mode=%s, provider=%s, type=%s, lifetime=%dm",
                mode, provider, proxy_type.value, lifetime,
            )
        return pool

    @property
    def enabled(self) -> bool:
        """Whether proxying is active."""
        if self.proxy_type == ProxyType.DIRECT:
            return False
        if self.proxy_type == ProxyType.DEDICATED:
            return bool(self.dedicated_url)
        return self.mode != "none" and bool(self.url_template)

    def get_proxy_url(self, user_id: str | None = None, *, override_type: ProxyType | None = None) -> str | None:
        """Get a proxy URL for a user.

        Returns None if proxying is disabled or type is direct.

        Args:
            user_id: User ID for sticky session assignment.
            override_type: Override the pool's default proxy type for this request.
        """
        active_type = override_type or self.proxy_type

        if active_type == ProxyType.DIRECT:
            return None

        if active_type == ProxyType.DEDICATED:
            return self.dedicated_url if self.dedicated_url else None

        # Residential mode
        if self.mode == "none" or not self.url_template:
            return None

        if self.mode == "single":
            session_id = "tablement-single"
        else:
            session_id = self._get_session_id(user_id or "anonymous")

        return self.url_template.replace("{session}", session_id)

    def set_proxy_type(self, proxy_type: ProxyType) -> None:
        """Switch the active proxy type at runtime (from admin panel)."""
        old = self.proxy_type
        self.proxy_type = proxy_type
        logger.info("Proxy type switched: %s → %s", old.value, proxy_type.value)

    def rotate_session(self, user_id: str) -> str | None:
        """Force-rotate a user's proxy session (new IP).

        Call this when you want a fresh IP for a user, e.g., after a
        rate-limit hit or before a high-stakes booking.
        """
        self._sessions.pop(user_id, None)
        return self.get_proxy_url(user_id)

    def _get_session_id(self, user_id: str) -> str:
        """Get or create a session ID for a user.

        Session IDs are stable for session_lifetime_minutes, then rotate
        automatically. This means a user keeps the same IP for ~30 minutes
        (configurable), mimicking a real person on their home WiFi.
        """
        now = time.time()
        max_age = self.session_lifetime_minutes * 60

        if user_id in self._sessions:
            session_id, created_at = self._sessions[user_id]
            if (now - created_at) < max_age:
                return session_id

        # Generate a new session ID
        # Use a hash of user_id + timestamp window so it's deterministic
        # within the window but changes when the window rolls over
        window = int(now // max_age)
        raw = f"{user_id}:{window}:{uuid.uuid4().hex[:8]}"
        session_id = hashlib.md5(raw.encode()).hexdigest()[:12]

        self._sessions[user_id] = (session_id, now)
        logger.debug("New proxy session for user %s: %s", user_id[:8], session_id)

        return session_id

    def get_stats(self) -> dict:
        """Return pool statistics for monitoring."""
        return {
            "mode": self.mode,
            "provider": self.provider,
            "proxy_type": self.proxy_type.value,
            "active_sessions": len(self._sessions),
            "session_lifetime_minutes": self.session_lifetime_minutes,
            "dedicated_configured": bool(self.dedicated_url),
        }

    async def test_connectivity(self, proxy_type: ProxyType | None = None) -> dict:
        """Test proxy connectivity by making a request through the proxy.

        Returns latency, IP, and geo information.
        """
        import httpx

        active_type = proxy_type or self.proxy_type
        proxy_url = self.get_proxy_url("connectivity-test", override_type=active_type)

        result = {
            "proxy_type": active_type.value,
            "proxy_url_masked": _mask_proxy_url(proxy_url) if proxy_url else None,
            "success": False,
            "latency_ms": 0,
            "ip": None,
            "error": None,
        }

        try:
            start = time.monotonic()
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=httpx.Timeout(10.0, connect=5.0),
            ) as client:
                # Use a lightweight IP check endpoint
                resp = await client.get("https://api.ipify.org?format=json")
                elapsed = time.monotonic() - start
                resp.raise_for_status()

                data = resp.json()
                result["success"] = True
                result["latency_ms"] = round(elapsed * 1000)
                result["ip"] = data.get("ip")

        except Exception as e:
            result["error"] = str(e)

        return result


def _mask_proxy_url(url: str | None) -> str | None:
    """Mask credentials in a proxy URL for safe display."""
    if not url:
        return None
    # Mask anything between :// and @
    import re
    return re.sub(r"://([^@]+)@", "://***:***@", url)


# Module-level singleton — initialized from env at import time
proxy_pool = ProxyPool.from_env()
