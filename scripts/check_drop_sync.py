"""Check drop timing consistency across curated_restaurants and scout_campaigns."""
import asyncio
from tablement.web import db


async def check():
    await db.init_supabase()
    client = db.get_service_client()

    # Get curated restaurants drop info
    cr = await client.table("curated_restaurants").select(
        "name, venue_id, drop_days_ahead, drop_hour, drop_minute"
    ).execute()
    curated = {r["venue_id"]: r for r in cr.data}

    # Get scout campaigns
    sc = await client.table("scout_campaigns").select(
        "venue_id, venue_name, type, days_ahead, active"
    ).execute()

    print("=== CURATED RESTAURANTS (source of truth) ===")
    for r in sorted(cr.data, key=lambda x: x.get("name", "")):
        h = r.get("drop_hour", "?")
        m = str(r.get("drop_minute", 0)).zfill(2)
        print(f"  {r['name']:30s} venue={r['venue_id']:6d}  drop={r['drop_days_ahead']}d ahead  at {h}:{m}")

    print()
    print("=== SCOUT CAMPAIGNS ===")
    for s in sorted(sc.data, key=lambda x: x.get("venue_name", "")):
        vid = s["venue_id"]
        ci = curated.get(vid)
        mismatch = ""
        if ci and ci["drop_days_ahead"] != s.get("days_ahead"):
            mismatch = f"  *** MISMATCH: curated says {ci['drop_days_ahead']}d ***"
        print(f"  {s.get('venue_name','?'):30s} venue={vid:6d}  type={s['type']:12s}  days_ahead={s.get('days_ahead')}  active={s['active']}{mismatch}")

    # Summary
    print()
    mismatches = []
    for s in sc.data:
        ci = curated.get(s["venue_id"])
        if ci and ci["drop_days_ahead"] != s.get("days_ahead"):
            mismatches.append((s.get("venue_name"), s.get("days_ahead"), ci["drop_days_ahead"]))
    if mismatches:
        print(f"FOUND {len(mismatches)} MISMATCHES:")
        for name, scout_days, curated_days in mismatches:
            print(f"  {name}: scout says {scout_days}d, curated says {curated_days}d")
    else:
        print("All scout campaigns match curated_restaurants. No mismatches.")


asyncio.run(check())
