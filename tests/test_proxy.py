"""Tests for the residential proxy pool system."""

import os
from unittest.mock import patch

import pytest

from tablement.proxy import ProxyPool


class TestProxyPool:
    def test_disabled_by_default(self):
        pool = ProxyPool()
        assert not pool.enabled
        assert pool.get_proxy_url("user-1") is None

    def test_single_mode_same_proxy_for_all(self):
        pool = ProxyPool(
            mode="single",
            url_template="http://user:pass_session-{session}@proxy.example.com:8080",
        )
        assert pool.enabled

        url1 = pool.get_proxy_url("user-1")
        url2 = pool.get_proxy_url("user-2")

        # In single mode, all users share the same session ID
        assert url1 == url2
        assert "tablement-single" in url1

    def test_pool_mode_unique_sessions_per_user(self):
        pool = ProxyPool(
            mode="pool",
            url_template="http://user:pass_session-{session}@proxy.example.com:8080",
        )

        url1 = pool.get_proxy_url("user-1")
        url2 = pool.get_proxy_url("user-2")

        # Different users should get different session IDs
        assert url1 != url2
        assert "{session}" not in url1  # Placeholder should be replaced
        assert "{session}" not in url2

    def test_sticky_sessions(self):
        pool = ProxyPool(
            mode="pool",
            url_template="http://user:pass_session-{session}@proxy.example.com:8080",
        )

        url1 = pool.get_proxy_url("user-sticky")
        url2 = pool.get_proxy_url("user-sticky")

        # Same user should get the same session within the lifetime
        assert url1 == url2

    def test_rotate_session(self):
        pool = ProxyPool(
            mode="pool",
            url_template="http://user:pass_session-{session}@proxy.example.com:8080",
        )

        url1 = pool.get_proxy_url("user-rotate")
        url2 = pool.rotate_session("user-rotate")

        # After rotation, the session ID should be different
        assert url1 != url2
        assert "proxy.example.com" in url2

    def test_no_session_placeholder_works(self):
        """If URL has no {session} placeholder, it still works (shared IP)."""
        pool = ProxyPool(
            mode="pool",
            url_template="http://user:pass@proxy.example.com:8080",
        )

        url = pool.get_proxy_url("user-1")
        assert url == "http://user:pass@proxy.example.com:8080"

    def test_from_env_defaults_to_none(self):
        with patch.dict(os.environ, {}, clear=True):
            pool = ProxyPool.from_env()
            assert pool.mode == "none"
            assert not pool.enabled

    def test_from_env_with_proxy_config(self):
        env = {
            "PROXY_MODE": "pool",
            "PROXY_URL": "http://user:pass_session-{session}@brd.superproxy.io:22225",
            "PROXY_PROVIDER": "brightdata",
            "PROXY_SESSION_LIFETIME_MINUTES": "60",
        }
        with patch.dict(os.environ, env, clear=True):
            pool = ProxyPool.from_env()
            assert pool.mode == "pool"
            assert pool.provider == "brightdata"
            assert pool.session_lifetime_minutes == 60
            assert pool.enabled

    def test_from_env_missing_url_falls_back(self):
        env = {"PROXY_MODE": "pool"}
        with patch.dict(os.environ, env, clear=True):
            pool = ProxyPool.from_env()
            assert pool.mode == "none"  # Falls back because URL is missing

    def test_get_stats(self):
        pool = ProxyPool(
            mode="pool",
            url_template="http://user:pass_session-{session}@proxy.example.com:8080",
            provider="brightdata",
        )
        pool.get_proxy_url("user-1")
        pool.get_proxy_url("user-2")

        stats = pool.get_stats()
        assert stats["mode"] == "pool"
        assert stats["provider"] == "brightdata"
        assert stats["active_sessions"] == 2

    def test_brightdata_url_format(self):
        """Verify Bright Data URL format works correctly."""
        pool = ProxyPool(
            mode="pool",
            url_template="http://brd-customer-XXX-zone-residential-session-{session}:PASSWORD@brd.superproxy.io:22225",
        )
        url = pool.get_proxy_url("user-1")
        assert "brd-customer-XXX-zone-residential-session-" in url
        assert "@brd.superproxy.io:22225" in url
        assert "{session}" not in url

    def test_iproyal_url_format(self):
        """Verify IPRoyal URL format works correctly."""
        pool = ProxyPool(
            mode="pool",
            url_template="http://USERNAME:PASSWORD_session-{session}_lifetime-30m@geo.iproyal.com:12321",
        )
        url = pool.get_proxy_url("user-1")
        assert "USERNAME:PASSWORD_session-" in url
        assert "_lifetime-30m@geo.iproyal.com:12321" in url
        assert "{session}" not in url
