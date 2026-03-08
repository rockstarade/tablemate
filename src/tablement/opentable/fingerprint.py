"""Mobile app fingerprint generation for OpenTable anti-detection.

Generates realistic OpenTable mobile app identities that match what
Akamai Bot Manager expects from real iOS/Android users.

Unlike Resy's browser fingerprints (sec-ch-ua, sec-fetch-*, etc.),
mobile API requests use simpler headers: just a User-Agent, Accept,
and Accept-Language. No Client Hints or Sec-Fetch headers.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Mobile app User-Agent pool — realistic OpenTable iOS/Android versions
# Update periodically as new app versions ship.
# ---------------------------------------------------------------------------

_IOS_DEVICES: list[dict[str, str]] = [
    # iPhone 16 Pro Max
    {"model": "iPhone17,2", "os": "18.3", "device_name": "iPhone 16 Pro Max"},
    # iPhone 16 Pro
    {"model": "iPhone17,1", "os": "18.3", "device_name": "iPhone 16 Pro"},
    # iPhone 16
    {"model": "iPhone17,3", "os": "18.2", "device_name": "iPhone 16"},
    # iPhone 15 Pro Max
    {"model": "iPhone16,2", "os": "18.3", "device_name": "iPhone 15 Pro Max"},
    # iPhone 15 Pro
    {"model": "iPhone16,1", "os": "18.2", "device_name": "iPhone 15 Pro"},
    # iPhone 15
    {"model": "iPhone15,4", "os": "18.1", "device_name": "iPhone 15"},
    # iPhone 14 Pro
    {"model": "iPhone15,3", "os": "17.7", "device_name": "iPhone 14 Pro"},
    # iPhone 14
    {"model": "iPhone14,7", "os": "17.7", "device_name": "iPhone 14"},
    # iPhone 13
    {"model": "iPhone14,5", "os": "17.6", "device_name": "iPhone 13"},
]

_ANDROID_DEVICES: list[dict[str, str]] = [
    {"model": "Pixel 9 Pro", "os": "15", "sdk": "35"},
    {"model": "Pixel 8 Pro", "os": "15", "sdk": "35"},
    {"model": "Pixel 8", "os": "14", "sdk": "34"},
    {"model": "SM-S928B", "os": "15", "sdk": "35"},  # Galaxy S25 Ultra
    {"model": "SM-S926B", "os": "14", "sdk": "34"},  # Galaxy S24+
    {"model": "SM-S921B", "os": "14", "sdk": "34"},  # Galaxy S24
]

_OT_APP_VERSIONS: list[str] = [
    "16.7.0", "16.6.2", "16.6.1", "16.5.0", "16.4.1", "16.3.0",
    "16.2.0", "16.1.1", "16.0.0",
]

_ACCEPT_LANGUAGES: list[str] = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,es;q=0.8",
    "en,en-US;q=0.9",
    "en-US",
]


# ---------------------------------------------------------------------------
# Mobile fingerprint dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MobileFingerprint:
    """Immutable mobile app identity for one session/user.

    Mimics the OpenTable iOS or Android app. Stays consistent for the
    lifetime of an ``OpenTableApiClient`` instance, just like a real
    phone doesn't change mid-session.
    """

    user_agent: str
    app_version: str
    platform: str  # "ios" or "android"
    accept_language: str

    def base_headers(self) -> dict[str, str]:
        """Headers that go on EVERY request to mobile-api.opentable.com."""
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Language": self.accept_language,
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }


# ---------------------------------------------------------------------------
# Pool (singleton pattern, same as Resy's FingerprintPool)
# ---------------------------------------------------------------------------


@dataclass
class MobileFingerprintPool:
    """Manages mobile fingerprint assignment for users.

    Each user gets a consistent fingerprint for their session,
    so all requests from the same user look like the same device.
    """

    _user_fingerprints: dict[str, MobileFingerprint] = field(default_factory=dict)

    def get_fingerprint(self, user_id: str | None = None) -> MobileFingerprint:
        """Get or create a mobile fingerprint for a user."""
        if user_id and user_id in self._user_fingerprints:
            return self._user_fingerprints[user_id]

        fp = _generate_mobile_fingerprint()

        if user_id:
            self._user_fingerprints[user_id] = fp

        return fp

    def rotate_fingerprint(self, user_id: str) -> MobileFingerprint:
        """Force-rotate a user's mobile fingerprint."""
        self._user_fingerprints.pop(user_id, None)
        return self.get_fingerprint(user_id)

    def clear(self) -> None:
        """Clear all cached fingerprints."""
        self._user_fingerprints.clear()


def _generate_mobile_fingerprint() -> MobileFingerprint:
    """Generate a random but internally-consistent mobile fingerprint."""
    app_version = random.choice(_OT_APP_VERSIONS)
    lang = random.choice(_ACCEPT_LANGUAGES)

    # 70% chance iOS (matches real OT user distribution), 30% Android
    if random.random() < 0.7:
        device = random.choice(_IOS_DEVICES)
        ua = (
            f"OpenTable/{app_version} "
            f"(iOS {device['os']}; {device['device_name']})"
        )
        platform = "ios"
    else:
        device = random.choice(_ANDROID_DEVICES)
        ua = (
            f"OpenTable/{app_version} "
            f"(Android {device['os']}; {device['model']})"
        )
        platform = "android"

    return MobileFingerprint(
        user_agent=ua,
        app_version=app_version,
        platform=platform,
        accept_language=lang,
    )


# Module-level singleton — shared across the app
mobile_fingerprint_pool = MobileFingerprintPool()
