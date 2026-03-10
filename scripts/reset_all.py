"""Nuclear reset: delete ALL user data — profiles, reservations, claims, scouts, payments.

Users will need to re-verify phone and re-link Resy on next visit.

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

    # 1) Delete ALL reservations (not just active — everything)
    print("=== Deleting ALL reservations ===")
    resp = await svc.table("reservations").select("id,venue_name,status").execute()
    print(f"  Found {len(resp.data)} reservations")
    for r in resp.data:
        print(f"  - {r.get('venue_name', '?')} ({r.get('status', '?')})")
    if resp.data:
        await (
            svc.table("reservations")
            .delete()
            .neq("id", "00000000-0000-0000-0000-000000000000")
            .execute()
        )
        print("  -> All deleted")

    # 2) Delete ALL slot claims
    print("\n=== Deleting ALL slot claims ===")
    resp = await svc.table("slot_claims").select("id").execute()
    print(f"  Found {len(resp.data)} claims")
    if resp.data:
        await (
            svc.table("slot_claims")
            .delete()
            .neq("id", "00000000-0000-0000-0000-000000000000")
            .execute()
        )
        print("  -> All deleted")

    # 3) Delete ALL scout campaigns
    print("\n=== Deleting ALL scout campaigns ===")
    resp = await svc.table("scout_campaigns").select("id,venue_name,type").execute()
    print(f"  Found {len(resp.data)} campaigns")
    for c in resp.data:
        print(f"  - {c.get('venue_name', '?')} ({c.get('type', '?')})")
    if resp.data:
        await (
            svc.table("scout_campaigns")
            .delete()
            .neq("id", "00000000-0000-0000-0000-000000000000")
            .execute()
        )
        print("  -> All deleted")

    # 4) Delete ALL payment methods
    print("\n=== Deleting ALL payment methods ===")
    resp = await svc.table("payment_methods").select("id").execute()
    print(f"  Found {len(resp.data)} payment methods")
    if resp.data:
        await (
            svc.table("payment_methods")
            .delete()
            .neq("id", "00000000-0000-0000-0000-000000000000")
            .execute()
        )
        print("  -> All deleted")

    # 5) Delete ALL transactions
    print("\n=== Deleting ALL transactions ===")
    resp = await svc.table("transactions").select("id").execute()
    print(f"  Found {len(resp.data)} transactions")
    if resp.data:
        await (
            svc.table("transactions")
            .delete()
            .neq("id", "00000000-0000-0000-0000-000000000000")
            .execute()
        )
        print("  -> All deleted")

    # 6) Delete ALL profiles
    print("\n=== Deleting ALL profiles ===")
    resp = await svc.table("profiles").select("id,resy_email,credits").execute()
    print(f"  Found {len(resp.data)} profiles")
    for p in resp.data:
        email = p.get("resy_email") or "None"
        credits = p.get("credits", 0)
        pid = p["id"][:8]
        print(f"  - {pid}... (resy={email}, credits={credits})")
    if resp.data:
        await (
            svc.table("profiles")
            .delete()
            .neq("id", "00000000-0000-0000-0000-000000000000")
            .execute()
        )
        print("  -> All deleted")

    # 7) Delete scout auxiliary data
    print("\n=== Cleaning scout auxiliary tables ===")
    for table_name in ["scout_events", "scout_snapshots", "scout_slot_sightings", "scout_errors"]:
        try:
            resp = await svc.table(table_name).select("id", count="exact").execute()
            count = resp.count if resp.count is not None else len(resp.data)
            if count > 0:
                await (
                    svc.table(table_name)
                    .delete()
                    .neq("id", "00000000-0000-0000-0000-000000000000")
                    .execute()
                )
                print(f"  {table_name}: {count} rows deleted")
            else:
                print(f"  {table_name}: empty")
        except Exception as e:
            print(f"  {table_name}: skipped ({e})")

    print("\n" + "=" * 50)
    print("NUCLEAR RESET COMPLETE.")
    print("All profiles, reservations, campaigns, claims, payments deleted.")
    print("Users will need to re-verify phone number and re-link Resy.")


if __name__ == "__main__":
    asyncio.run(main())
