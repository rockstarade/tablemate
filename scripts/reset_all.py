"""One-shot cleanup: cancel reservations, deactivate scouts, clear Resy creds, clear claims.

Run on server with:
    cd ~/tablement && .venv/bin/python scripts/reset_all.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


async def main():
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

    from tablement.web.db import init_supabase, get_service_client

    await init_supabase()
    svc = get_service_client()

    # 1) Cancel all active reservations
    print("=== Cancelling all active reservations ===")
    resp = (
        await svc.table("reservations")
        .select("id,status,venue_name")
        .in_("status", ["pending", "scheduled", "monitoring", "sniping"])
        .execute()
    )
    print(f"  Found {len(resp.data)} active reservations")
    if resp.data:
        await (
            svc.table("reservations")
            .update({"status": "cancelled", "error": "Admin reset"})
            .in_("status", ["pending", "scheduled", "monitoring", "sniping"])
            .execute()
        )
        for r in resp.data:
            vname = r.get("venue_name", "?")
            status = r.get("status", "?")
            print(f"  - {vname} ({status}) -> cancelled")

    # 2) Deactivate all scout campaigns
    print("\n=== Deactivating all scout campaigns ===")
    resp = await svc.table("scout_campaigns").select("*").execute()
    print(f"  Found {len(resp.data)} campaigns")
    if resp.data:
        await (
            svc.table("scout_campaigns")
            .update({"active": False})
            .neq("id", "00000000-0000-0000-0000-000000000000")
            .execute()
        )
        for c in resp.data:
            vname = c.get("venue_name", "?")
            ctype = c.get("type", "?")
            active = c.get("active", "?")
            print(f"  - {vname} ({ctype}, was active={active}) -> deactivated")

    # 3) Clear all active slot claims
    print("\n=== Cancelling all active slot claims ===")
    resp = (
        await svc.table("slot_claims").select("id").eq("status", "active").execute()
    )
    print(f"  Found {len(resp.data)} active claims")
    if resp.data:
        await (
            svc.table("slot_claims")
            .update({"status": "cancelled"})
            .eq("status", "active")
            .execute()
        )
        print("  -> All cancelled")

    # 4) Clear Resy credentials from ALL profiles
    print("\n=== Clearing Resy credentials from all profiles ===")
    resp = await svc.table("profiles").select("id,resy_email,credits").execute()
    print(f"  Found {len(resp.data)} profiles")
    for p in resp.data:
        email = p.get("resy_email") or "None"
        credits = p.get("credits", 0)
        pid = p["id"][:8]
        print(f"  - {pid}... (resy={email}, credits={credits})")

    await (
        svc.table("profiles")
        .update(
            {
                "resy_email": None,
                "resy_password_encrypted": None,
                "resy_auth_token": None,
                "resy_phone": None,
                "resy_token_expires_at": None,
            }
        )
        .neq("id", "00000000-0000-0000-0000-000000000000")
        .execute()
    )
    print("  -> All Resy credentials cleared")

    print("\n" + "=" * 50)
    print("DONE. All data wiped clean.")
    print("Users will need to re-link Resy email + password on next visit.")


if __name__ == "__main__":
    asyncio.run(main())
