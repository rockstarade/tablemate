"""Tests for the async Resy API client."""

import json

import httpx
import pytest
import respx

from tablement.api import API_KEY, ResyApiClient
from tablement.fingerprint import BrowserFingerprint, _generate_fingerprint


@pytest.mark.asyncio
class TestResyApiClient:
    async def test_authenticate_success(self, sample_auth_response):
        with respx.mock:
            respx.post("https://api.resy.com/3/auth/password").mock(
                return_value=httpx.Response(200, json=sample_auth_response)
            )

            async with ResyApiClient() as client:
                resp = await client.authenticate("test@example.com", "password123")

            assert resp.token == sample_auth_response["token"]
            assert len(resp.payment_methods) == 1
            assert resp.payment_methods[0].id == 67890

    async def test_authenticate_failure(self):
        with respx.mock:
            respx.post("https://api.resy.com/3/auth/password").mock(
                return_value=httpx.Response(401, json={"message": "Invalid credentials"})
            )

            async with ResyApiClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await client.authenticate("bad@example.com", "wrong")

    async def test_find_slots(self, sample_find_response):
        with respx.mock:
            respx.get("https://api.resy.com/4/find").mock(
                return_value=httpx.Response(200, json=sample_find_response)
            )

            async with ResyApiClient(auth_token="test_token") as client:
                slots = await client.find_slots(
                    venue_id=12345, day="2026-03-26", party_size=2
                )

            assert len(slots) == 5
            assert slots[0].config.type == "Dining Room"
            assert slots[2].config.type == "Bar"

    async def test_find_slots_empty(self):
        with respx.mock:
            respx.get("https://api.resy.com/4/find").mock(
                return_value=httpx.Response(200, json={"results": {"venues": []}})
            )

            async with ResyApiClient(auth_token="test_token") as client:
                slots = await client.find_slots(
                    venue_id=12345, day="2026-03-26", party_size=2
                )

            assert slots == []

    async def test_get_details(self, sample_details_response):
        with respx.mock:
            respx.get("https://api.resy.com/3/details").mock(
                return_value=httpx.Response(200, json=sample_details_response)
            )

            async with ResyApiClient(auth_token="test_token") as client:
                resp = await client.get_details(
                    config_id="config_token_1",
                    day="2026-03-26",
                    party_size=2,
                )

            assert resp.book_token.value == "book_token_abc123"

    async def test_book_get_primary(self, sample_book_response):
        """book() tries GET first (CAPTCHA bypass) — succeeds on GET."""
        with respx.mock:
            get_route = respx.get("https://api.resy.com/3/book").mock(
                return_value=httpx.Response(200, json=sample_book_response)
            )
            post_route = respx.post("https://api.resy.com/3/book").mock(
                return_value=httpx.Response(200, json=sample_book_response)
            )

            async with ResyApiClient(auth_token="test_token") as client:
                resp = await client.book(
                    book_token="book_token_abc123",
                    payment_method_id=67890,
                )

            assert resp.resy_token == "resy_conf_xyz789"

            # GET was used (CAPTCHA bypass), POST was NOT called
            assert get_route.called
            assert not post_route.called

            # Verify GET params include the booking data
            request = get_route.calls[0].request
            assert "book_token=book_token_abc123" in str(request.url)
            assert "struct_payment_method" in str(request.url)

    async def test_book_post_fallback(self, sample_book_response):
        """book() falls back to POST when GET fails."""
        with respx.mock:
            get_route = respx.get("https://api.resy.com/3/book").mock(
                return_value=httpx.Response(500, json={"message": "Server error"})
            )
            post_route = respx.post("https://api.resy.com/3/book").mock(
                return_value=httpx.Response(200, json=sample_book_response)
            )

            async with ResyApiClient(auth_token="test_token") as client:
                resp = await client.book(
                    book_token="book_token_abc123",
                    payment_method_id=67890,
                )

            assert resp.resy_token == "resy_conf_xyz789"

            # GET was tried first, then POST fallback succeeded
            assert get_route.called
            assert post_route.called

            # Verify POST body was form-encoded correctly
            request = post_route.calls[0].request
            body = request.content.decode()
            assert "book_token=book_token_abc123" in body
            assert "struct_payment_method" in body

    async def test_auth_headers_set(self, sample_find_response):
        with respx.mock:
            route = respx.get("https://api.resy.com/4/find").mock(
                return_value=httpx.Response(200, json=sample_find_response)
            )

            async with ResyApiClient(auth_token="my_secret_token") as client:
                await client.find_slots(venue_id=1, day="2026-01-01", party_size=1)

            request = route.calls[0].request
            assert f'ResyAPI api_key="{API_KEY}"' in request.headers["authorization"]
            assert request.headers["x-resy-auth-token"] == "my_secret_token"
            assert request.headers["x-resy-universal-auth"] == "my_secret_token"

    async def test_book_uses_widget_origin(self, sample_book_response):
        """The /3/book endpoint requires widgets.resy.com as origin."""
        with respx.mock:
            route = respx.get("https://api.resy.com/3/book").mock(
                return_value=httpx.Response(200, json=sample_book_response)
            )

            async with ResyApiClient(auth_token="test_token") as client:
                await client.book(book_token="tok", payment_method_id=1)

            request = route.calls[0].request
            assert request.headers["origin"] == "https://widgets.resy.com"

    # ------------------------------------------------------------------
    # Anti-detection layer tests
    # ------------------------------------------------------------------

    async def test_fingerprint_headers_present(self, sample_find_response):
        """Layer 3: Client should include sec-ch-ua and sec-fetch-* headers."""
        with respx.mock:
            route = respx.get("https://api.resy.com/4/find").mock(
                return_value=httpx.Response(200, json=sample_find_response)
            )

            async with ResyApiClient(auth_token="test_token") as client:
                await client.find_slots(venue_id=1, day="2026-01-01", party_size=1)

            request = route.calls[0].request
            # Chrome Client Hints
            assert "sec-ch-ua" in request.headers
            assert "sec-ch-ua-mobile" in request.headers
            assert "sec-ch-ua-platform" in request.headers
            # Sec-Fetch headers
            assert request.headers.get("sec-fetch-site") == "same-site"
            assert request.headers.get("sec-fetch-mode") == "cors"
            assert request.headers.get("sec-fetch-dest") == "empty"
            # Accept-Language
            assert "accept-language" in request.headers

    async def test_browsing_uses_resy_origin(self, sample_find_response):
        """Layer 3: Browsing endpoints use resy.com as origin."""
        with respx.mock:
            route = respx.get("https://api.resy.com/4/find").mock(
                return_value=httpx.Response(200, json=sample_find_response)
            )

            async with ResyApiClient(auth_token="test_token") as client:
                await client.find_slots(venue_id=1, day="2026-01-01", party_size=1)

            request = route.calls[0].request
            assert request.headers["origin"] == "https://resy.com"
            assert request.headers["referer"] == "https://resy.com/"

    async def test_booking_uses_widget_origin_with_sec_fetch(self, sample_book_response):
        """Layer 3: Booking endpoint uses widgets.resy.com + proper sec-fetch-*."""
        with respx.mock:
            route = respx.get("https://api.resy.com/3/book").mock(
                return_value=httpx.Response(200, json=sample_book_response)
            )

            async with ResyApiClient(auth_token="test_token") as client:
                await client.book(book_token="tok", payment_method_id=1)

            request = route.calls[0].request
            assert request.headers["origin"] == "https://widgets.resy.com"
            assert request.headers["referer"] == "https://widgets.resy.com/"
            assert request.headers.get("sec-fetch-site") == "same-site"

    async def test_explicit_fingerprint_used(self, sample_find_response):
        """Layer 3: An explicitly provided fingerprint is used over auto-generated."""
        fp = BrowserFingerprint(
            user_agent="TestBot/1.0",
            sec_ch_ua='"TestBrowser";v="99"',
            sec_ch_ua_mobile="?0",
            sec_ch_ua_platform='"TestOS"',
            accept_language="en-US",
        )

        with respx.mock:
            route = respx.get("https://api.resy.com/4/find").mock(
                return_value=httpx.Response(200, json=sample_find_response)
            )

            async with ResyApiClient(
                auth_token="test_token", fingerprint=fp
            ) as client:
                await client.find_slots(venue_id=1, day="2026-01-01", party_size=1)

            request = route.calls[0].request
            assert request.headers["user-agent"] == "TestBot/1.0"
            assert request.headers["sec-ch-ua"] == '"TestBrowser";v="99"'

    async def test_scout_mode_no_auth_token(self, sample_find_response):
        """Layer 2: Scout mode should NOT include auth token headers."""
        with respx.mock:
            route = respx.get("https://api.resy.com/4/find").mock(
                return_value=httpx.Response(200, json=sample_find_response)
            )

            async with ResyApiClient(
                auth_token="should_not_appear", scout=True
            ) as client:
                await client.find_slots(venue_id=1, day="2026-01-01", party_size=1)

            request = route.calls[0].request
            # Auth token should NOT be in headers (scout mode)
            assert "x-resy-auth-token" not in request.headers
            assert "x-resy-universal-auth" not in request.headers
            # But API key should still be present
            assert f'ResyAPI api_key="{API_KEY}"' in request.headers["authorization"]

    async def test_scout_mode_exits_on_set_auth_token(self, sample_find_response):
        """Layer 2: Calling set_auth_token() should exit scout mode."""
        with respx.mock:
            route = respx.get("https://api.resy.com/4/find").mock(
                return_value=httpx.Response(200, json=sample_find_response)
            )

            async with ResyApiClient(scout=True) as client:
                # Initially in scout mode
                client.set_auth_token("new_token")

                await client.find_slots(venue_id=1, day="2026-01-01", party_size=1)

            request = route.calls[0].request
            # After set_auth_token, scout mode is off
            assert request.headers["x-resy-auth-token"] == "new_token"
            assert request.headers["x-resy-universal-auth"] == "new_token"

    async def test_user_id_produces_consistent_fingerprint(self, sample_find_response):
        """Layer 3: Same user_id should get the same fingerprint across clients."""
        with respx.mock:
            route = respx.get("https://api.resy.com/4/find").mock(
                return_value=httpx.Response(200, json=sample_find_response)
            )

            # First client
            async with ResyApiClient(user_id="user-consistent-test") as c1:
                await c1.find_slots(venue_id=1, day="2026-01-01", party_size=1)

            # Second client with same user_id
            async with ResyApiClient(user_id="user-consistent-test") as c2:
                await c2.find_slots(venue_id=1, day="2026-01-01", party_size=1)

            r1 = route.calls[0].request
            r2 = route.calls[1].request

            # Same user_id → same User-Agent
            assert r1.headers["user-agent"] == r2.headers["user-agent"]
            assert r1.headers["sec-ch-ua"] == r2.headers["sec-ch-ua"]
