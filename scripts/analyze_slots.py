#!/usr/bin/env python3
"""Analyze scout data and generate known_slots for all restaurants."""
import os, json, sys
from collections import defaultdict
from supabase import create_client

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(url, key)

# Step 1: Get all curated restaurants
resp = sb.table("curated_restaurants").select("venue_id,name,known_slots,service_start,service_end,slot_interval").execute()
restaurants = {r["venue_id"]: r for r in resp.data}

# Step 2: Get ALL slot_snapshots with slots_json (from collector/scouts)
all_snapshots = []
offset = 0
while True:
    resp = sb.table("slot_snapshots").select("venue_id,slots_json,slot_count").gt("slot_count", 0).range(offset, offset + 999).execute()
    if not resp.data:
        break
    all_snapshots.extend(resp.data)
    if len(resp.data) < 1000:
        break
    offset += 1000

# Step 3: Get ALL scout_snapshots with slots_json
offset = 0
while True:
    resp = sb.table("scout_snapshots").select("venue_id,slots_json,slot_count").gt("slot_count", 0).range(offset, offset + 999).execute()
    if not resp.data:
        break
    all_snapshots.extend(resp.data)
    if len(resp.data) < 1000:
        break
    offset += 1000

print(f"Total snapshot rows with slot data: {len(all_snapshots)}")

# Step 4: Aggregate unique times per venue
venue_times = defaultdict(set)
venue_types = defaultdict(set)
venue_snapshot_count = defaultdict(int)

for row in all_snapshots:
    vid = row["venue_id"]
    sj = row.get("slots_json")
    if not sj or not isinstance(sj, list):
        continue
    venue_snapshot_count[vid] += 1
    for s in sj:
        if isinstance(s, dict):
            t = s.get("time", "")
            if " " in t:
                t = t.split(" ")[1][:5]
            elif "T" in t:
                t = t.split("T")[1][:5]
            if t and len(t) == 5:
                venue_times[vid].add(t)
            st = s.get("type", "")
            if st:
                venue_types[vid].add(st.strip())

# Step 5: Process each restaurant
print(f"\nVenues with observed slot data: {len(venue_times)}")
print("=" * 80)
print("RESTAURANT-BY-RESTAURANT ANALYSIS")
print("=" * 80)

updates = {}
flags = []

for vid in sorted(venue_times.keys()):
    times = sorted(venue_times[vid])
    types = sorted(venue_types[vid])
    count = venue_snapshot_count[vid]

    r = restaurants.get(vid)
    if not r:
        flags.append(f"WARNING: Venue {vid} has scout data but NOT in curated_restaurants!")
        continue

    name = r["name"]
    svc_start = r.get("service_start", "17:00")
    svc_end = r.get("service_end", "22:00")
    interval = r.get("slot_interval", 15)
    existing = r.get("known_slots")

    # Check alignment: are observed times within service window?
    svc_s = int(svc_start.split(":")[0]) * 60 + int(svc_start.split(":")[1])
    svc_e = int(svc_end.split(":")[0]) * 60 + int(svc_end.split(":")[1])
    if svc_e < svc_s:
        svc_e += 24 * 60

    out_of_range = []
    for t in times:
        mins = int(t.split(":")[0]) * 60 + int(t.split(":")[1])
        if mins < svc_s - 120 or (mins > svc_e + 60 and svc_e < 24 * 60):
            out_of_range.append(t)

    print("-" * 60)
    print(f"  {vid:>6} | {name}")
    print(f"         Service: {svc_start}-{svc_end} (interval: {interval}min)")
    print(f"         Snapshots: {count} | Types: {types}")
    print(f"         Observed times ({len(times)}): {times}")

    if out_of_range:
        note = f"WARNING: Venue {vid} ({name}): Times outside service window: {out_of_range}"
        flags.append(note)
        print(f"         OUT OF RANGE: {out_of_range}")

    # Also update service_start/service_end if observed data extends beyond
    earliest = times[0]
    latest = times[-1]
    update_svc = {}

    earliest_mins = int(earliest.split(":")[0]) * 60 + int(earliest.split(":")[1])
    latest_mins = int(latest.split(":")[0]) * 60 + int(latest.split(":")[1])

    if earliest_mins < svc_s:
        update_svc["service_start"] = earliest
        print(f"         -> UPDATE service_start: {svc_start} -> {earliest}")
    if latest_mins > svc_e and svc_e < 24 * 60:
        update_svc["service_end"] = latest
        print(f"         -> UPDATE service_end: {svc_end} -> {latest}")

    if existing:
        if set(existing) == set(times):
            print(f"         ALREADY MATCHES known_slots")
        else:
            diff_new = sorted(set(times) - set(existing))
            diff_old = sorted(set(existing) - set(times))
            if diff_new:
                print(f"         NEW times not in existing: {diff_new}")
            if diff_old:
                print(f"         Existing times NOT observed: {diff_old}")

    updates[vid] = {"name": name, "known_slots": times, "types": types, "svc_update": update_svc}

print()
print("=" * 80)
print("FLAGS & WARNINGS")
print("=" * 80)
for f in flags:
    print(f"  {f}")

print()
print("=" * 80)
print(f"SUMMARY: {len(updates)} restaurants to update with known_slots")
print("=" * 80)
for vid, data in sorted(updates.items()):
    print(f"  {vid:>6} | {data['name']:<35} | {len(data['known_slots'])} times | types: {data['types']}")

# Step 6: Apply updates
if "--apply" in sys.argv:
    print()
    print("=" * 80)
    print("APPLYING UPDATES...")
    print("=" * 80)

    for vid, data in sorted(updates.items()):
        slots = data["known_slots"]
        svc = data["svc_update"]

        # Build the update payload
        update_data = {"known_slots": slots}
        if svc.get("service_start"):
            update_data["service_start"] = svc["service_start"]
        if svc.get("service_end"):
            update_data["service_end"] = svc["service_end"]

        try:
            resp = sb.table("curated_restaurants").update(update_data).eq("venue_id", vid).execute()
            print(f"  UPDATED {vid:>6} | {data['name']:<35} | {len(slots)} slots")
        except Exception as e:
            print(f"  FAILED  {vid:>6} | {data['name']:<35} | Error: {e}")

    print("\nDone! All updates applied.")
else:
    print("\nDry run complete. Run with --apply to apply updates.")
