"""Browser fingerprint generation for anti-detection.

Layer 3 of the anti-detection system. Generates realistic browser fingerprints
that match what Resy's WAF (Imperva/Incapsula) expects to see from real users.

Key elements:
- Chrome Client Hints (sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-platform)
- Sec-Fetch-* headers (site, mode, dest) per endpoint context
- Realistic User-Agent strings from a pool of real Chrome versions
- Per-endpoint Origin/Referer switching (resy.com vs widgets.resy.com)
- Accept-Language and Accept-Encoding matching real browsers
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# User-Agent pool — real Chrome versions on macOS / Windows / Linux
# Update these periodically (every ~6 weeks when Chrome ships a new major)
# ---------------------------------------------------------------------------

_CHROME_VERSIONS: list[dict[str, str]] = [
    # Chrome 131 — macOS
    {
        "ua": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "ch_platform": '"macOS"',
        "ch_mobile": "?0",
    },
    # Chrome 130 — macOS
    {
        "ua": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "ch_ua": '"Google Chrome";v="130", "Chromium";v="130", "Not_A Brand";v="24"',
        "ch_platform": '"macOS"',
        "ch_mobile": "?0",
    },
    # Chrome 131 — Windows
    {
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "ch_platform": '"Windows"',
        "ch_mobile": "?0",
    },
    # Chrome 130 — Windows
    {
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "ch_ua": '"Google Chrome";v="130", "Chromium";v="130", "Not_A Brand";v="24"',
        "ch_platform": '"Windows"',
        "ch_mobile": "?0",
    },
    # Chrome 131 — Linux
    {
        "ua": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "ch_platform": '"Linux"',
        "ch_mobile": "?0",
    },
    # Chrome 129 — macOS (slightly older, still common)
    {
        "ua": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Safari/537.36"
        ),
        "ch_ua": '"Google Chrome";v="129", "Chromium";v="129", "Not_A Brand";v="24"',
        "ch_platform": '"macOS"',
        "ch_mobile": "?0",
    },
]


# ---------------------------------------------------------------------------
# Accept-Language variations — real browser distributions
# ---------------------------------------------------------------------------

_ACCEPT_LANGUAGES: list[str] = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,es;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "en,en-US;q=0.9",
]


# ---------------------------------------------------------------------------
# Fingerprint dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrowserFingerprint:
    """Immutable browser identity for one session/user.

    Created once per ResyApiClient instance, stays consistent for the
    lifetime of that connection (a real user doesn't switch browsers mid-session).
    """

    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_mobile: str
    sec_ch_ua_platform: str
    accept_language: str

    def base_headers(self) -> dict[str, str]:
        """Headers that go on EVERY request (identity headers)."""
        return {
            "User-Agent": self.user_agent,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": self.sec_ch_ua_mobile,
            "sec-ch-ua-platform": self.sec_ch_ua_platform,
            "Accept-Language": self.accept_language,
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept": "application/json, text/plain, */*",
        }

    def browsing_headers(self) -> dict[str, str]:
        """Extra headers for normal API browsing (find, details, auth).

        These come from resy.com — the main website making API calls.
        """
        return {
            "Origin": "https://resy.com",
            "X-Origin": "https://resy.com",
            "Referer": "https://resy.com/",
            "sec-fetch-site": "same-site",  # resy.com → api.resy.com
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }

    def widget_headers(self) -> dict[str, str]:
        """Extra headers for the booking widget (book endpoint).

        The booking flow comes from widgets.resy.com — a separate origin
        used by Resy's embedded booking widget.
        """
        return {
            "Origin": "https://widgets.resy.com",
            "X-Origin": "https://widgets.resy.com",
            "Referer": "https://widgets.resy.com/",
            "sec-fetch-site": "same-site",  # widgets.resy.com → api.resy.com
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "Cache-Control": "no-cache",
        }


@dataclass
class FingerprintPool:
    """Manages fingerprint assignment for users.

    Each user gets a consistent fingerprint for their session, so all
    requests from the same user look like the same browser.
    """

    _user_fingerprints: dict[str, BrowserFingerprint] = field(default_factory=dict)

    def get_fingerprint(self, user_id: str | None = None) -> BrowserFingerprint:
        """Get or create a fingerprint for a user.

        If user_id is provided, returns the same fingerprint for repeated calls
        (consistency within a user session). If None, generates a fresh random one.
        """
        if user_id and user_id in self._user_fingerprints:
            return self._user_fingerprints[user_id]

        fp = _generate_fingerprint()

        if user_id:
            self._user_fingerprints[user_id] = fp

        return fp

    def rotate_fingerprint(self, user_id: str) -> BrowserFingerprint:
        """Force-rotate a user's fingerprint (e.g., after a long time).

        Call this periodically (e.g., every 24h) to simulate a user
        updating their browser.
        """
        self._user_fingerprints.pop(user_id, None)
        return self.get_fingerprint(user_id)

    def clear(self) -> None:
        """Clear all cached fingerprints."""
        self._user_fingerprints.clear()


def _generate_fingerprint() -> BrowserFingerprint:
    """Generate a random but internally-consistent browser fingerprint."""
    chrome = random.choice(_CHROME_VERSIONS)
    lang = random.choice(_ACCEPT_LANGUAGES)

    return BrowserFingerprint(
        user_agent=chrome["ua"],
        sec_ch_ua=chrome["ch_ua"],
        sec_ch_ua_mobile=chrome["ch_mobile"],
        sec_ch_ua_platform=chrome["ch_platform"],
        accept_language=lang,
    )


# Module-level singleton — shared across the app
fingerprint_pool = FingerprintPool()
