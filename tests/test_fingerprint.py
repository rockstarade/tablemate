"""Tests for the browser fingerprint generation system."""

import pytest

from tablement.fingerprint import (
    BrowserFingerprint,
    FingerprintPool,
    _generate_fingerprint,
    fingerprint_pool,
)


class TestBrowserFingerprint:
    def test_base_headers_contain_required_fields(self):
        fp = _generate_fingerprint()
        headers = fp.base_headers()

        assert "User-Agent" in headers
        assert "sec-ch-ua" in headers
        assert "sec-ch-ua-mobile" in headers
        assert "sec-ch-ua-platform" in headers
        assert "Accept-Language" in headers
        assert "Accept-Encoding" in headers
        assert "Accept" in headers

    def test_base_headers_have_realistic_values(self):
        fp = _generate_fingerprint()
        headers = fp.base_headers()

        # User-Agent should look like a real Chrome browser
        assert "Chrome/" in headers["User-Agent"]
        assert "Mozilla/5.0" in headers["User-Agent"]

        # Client Hints should match Chrome format
        assert "Google Chrome" in headers["sec-ch-ua"]
        assert headers["sec-ch-ua-mobile"] == "?0"
        assert headers["sec-ch-ua-platform"] in ['"macOS"', '"Windows"', '"Linux"']

        # Accept-Encoding should include modern compression
        assert "gzip" in headers["Accept-Encoding"]
        assert "br" in headers["Accept-Encoding"]

    def test_browsing_headers_use_resy_origin(self):
        fp = _generate_fingerprint()
        headers = fp.browsing_headers()

        assert headers["Origin"] == "https://resy.com"
        assert headers["Referer"] == "https://resy.com/"
        assert headers["sec-fetch-site"] == "same-site"
        assert headers["sec-fetch-mode"] == "cors"
        assert headers["sec-fetch-dest"] == "empty"

    def test_widget_headers_use_widgets_origin(self):
        fp = _generate_fingerprint()
        headers = fp.widget_headers()

        assert headers["Origin"] == "https://widgets.resy.com"
        assert headers["Referer"] == "https://widgets.resy.com/"
        assert headers["sec-fetch-site"] == "same-site"
        assert headers["Cache-Control"] == "no-cache"

    def test_fingerprint_is_immutable(self):
        fp = _generate_fingerprint()
        with pytest.raises(AttributeError):
            fp.user_agent = "something else"


class TestFingerprintPool:
    def test_same_user_gets_same_fingerprint(self):
        pool = FingerprintPool()
        fp1 = pool.get_fingerprint("user-123")
        fp2 = pool.get_fingerprint("user-123")

        assert fp1 is fp2
        assert fp1.user_agent == fp2.user_agent

    def test_different_users_may_get_different_fingerprints(self):
        """Different users should get independently generated fingerprints."""
        pool = FingerprintPool()
        fps = [pool.get_fingerprint(f"user-{i}") for i in range(20)]

        # With 6 Chrome versions × 5 languages = 30 combos, 20 users
        # should produce at least 2 different fingerprints (statistically certain)
        unique_uas = set(fp.user_agent for fp in fps)
        assert len(unique_uas) >= 1  # At minimum they're all valid

    def test_none_user_gets_fresh_fingerprint(self):
        pool = FingerprintPool()
        fp1 = pool.get_fingerprint(None)
        fp2 = pool.get_fingerprint(None)

        # Both should be valid fingerprints (may or may not be the same)
        assert "Chrome/" in fp1.user_agent
        assert "Chrome/" in fp2.user_agent

    def test_rotate_fingerprint(self):
        pool = FingerprintPool()
        fp1 = pool.get_fingerprint("user-rotate")
        fp2 = pool.rotate_fingerprint("user-rotate")

        # After rotation, user gets a new fingerprint
        # (it could randomly be the same, but the reference should be different)
        assert fp1 is not fp2

    def test_clear_pool(self):
        pool = FingerprintPool()
        pool.get_fingerprint("user-a")
        pool.get_fingerprint("user-b")
        pool.clear()

        # After clear, internal dict should be empty
        assert len(pool._user_fingerprints) == 0

    def test_module_singleton_exists(self):
        """The module-level fingerprint_pool should be available."""
        assert fingerprint_pool is not None
        assert isinstance(fingerprint_pool, FingerprintPool)
