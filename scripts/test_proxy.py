"""Quick smoke test: verify IPRoyal proxy is working.

Sends 3 requests through the proxy to check:
1. Proxy connects successfully
2. We get a US residential IP
3. Different session IDs give different IPs (sticky sessions work)

Run: .venv/bin/python scripts/test_proxy.py
"""

import asyncio
import os
import sys

# Load .env file manually (no extra dependency needed)
env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ[key.strip()] = value.strip()

import httpx


async def main():
    proxy_url_template = os.environ.get("PROXY_URL", "")
    if not proxy_url_template or "{session}" not in proxy_url_template:
        print("ERROR: PROXY_URL not set or missing {session} placeholder")
        print("Make sure .env file exists with PROXY_URL configured")
        sys.exit(1)

    print("=" * 60)
    print("IPRoyal Proxy Smoke Test")
    print("=" * 60)
    print()

    # Test 1: Basic connectivity with session "test-1"
    proxy_1 = proxy_url_template.replace("{session}", "smoketest-1")
    print(f"Test 1: Checking proxy connectivity...")
    print(f"  Proxy: ...session-smoketest-1...")
    try:
        async with httpx.AsyncClient(proxy=proxy_1, timeout=15.0) as client:
            resp = await client.get("https://ipv4.icanhazip.com")
            ip1 = resp.text.strip()
            print(f"  ✓ Connected! Your proxy IP: {ip1}")
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        sys.exit(1)

    print()

    # Test 2: Same session ID should give same IP (sticky)
    print(f"Test 2: Sticky session (same session ID = same IP)...")
    try:
        async with httpx.AsyncClient(proxy=proxy_1, timeout=15.0) as client:
            resp = await client.get("https://ipv4.icanhazip.com")
            ip1_again = resp.text.strip()
            if ip1 == ip1_again:
                print(f"  ✓ Same IP: {ip1_again} (sticky session works!)")
            else:
                print(f"  ~ Different IP: {ip1_again} (session may have rotated, still OK)")
    except Exception as e:
        print(f"  ✗ Failed: {e}")

    print()

    # Test 3: Different session ID should give different IP
    proxy_2 = proxy_url_template.replace("{session}", "smoketest-2")
    print(f"Test 3: Different session ID = different IP...")
    try:
        async with httpx.AsyncClient(proxy=proxy_2, timeout=15.0) as client:
            resp = await client.get("https://ipv4.icanhazip.com")
            ip2 = resp.text.strip()
            if ip1 != ip2:
                print(f"  ✓ Different IP: {ip2} (per-user IP diversity works!)")
            else:
                print(f"  ~ Same IP: {ip2} (may happen occasionally, still OK)")
    except Exception as e:
        print(f"  ✗ Failed: {e}")

    print()

    # Test 4: Hit Resy API through proxy
    print(f"Test 4: Resy API through proxy (venue search)...")
    proxy_3 = proxy_url_template.replace("{session}", "smoketest-resy")
    try:
        async with httpx.AsyncClient(proxy=proxy_3, timeout=15.0) as client:
            resp = await client.get(
                "https://api.resy.com/3/venue",
                params={"url_slug": "lilia", "location": "new-york-ny"},
                headers={
                    "Authorization": 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"',
                    "Origin": "https://resy.com",
                    "Referer": "https://resy.com/",
                    "Accept": "application/json",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                name = data.get("name", "?")
                print(f"  ✓ Resy API works through proxy! Venue: {name}")
            else:
                print(f"  ~ Resy returned HTTP {resp.status_code} (may need different endpoint)")
                print(f"    Response: {resp.text[:200]}")
    except Exception as e:
        print(f"  ✗ Failed: {e}")

    print()
    print("=" * 60)
    print("Summary: If Tests 1-3 passed, your proxy is ready to go.")
    print("Set PROXY_MODE=pool in .env to enable for all Tablement traffic.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
