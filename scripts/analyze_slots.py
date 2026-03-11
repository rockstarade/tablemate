#!/usr/bin/env python3
"""Analyze ALL data sources and generate known_slots for all restaurants.

Queries 6 tables:
  1. slot_snapshots     — collector/snipe polling (slots_json)
  2. scout_snapshots    — continuous scout captures (slots_json)
  3. scout_slot_sightings — per-slot lifecycle tracking (slot_time)
  4. drop_observations  — drop detection records (first/last_slot_time)
  5. scout_events       — availability change events (slots_added/removed)
  6. slot_claims        — user-claimed time preferences (preferred_time)
"""
import os, json, sys
from collections import defaultdict
from supabase import create_client

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_SERVICE_KEY"]
sb = create_client(url, key)


def extract_hhmm(t):
    """Extract HH:MM from various time formats."""
    if not t or not isinstance(t, str):
        return None
    t = t.strip()
    if " " in t:
        t = t.split(" ")[1][:5]
    elif "T" in t:
        t = t.split("T")[1][:5]
    else:
        t = t[:5]
    # Validate HH:MM format
    if len(t) == 5 and t[2] == ":" and t[:2].isdigit() and t[3:].isdigit():
        h, m = int(t[:2]), int(t[3:])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return t
    return None


def paginated_query(table, select, filters=None):
    """Fetch all rows with pagination."""
    rows = []
    offset = 0
    while True:
        q = sb.table(table).select(select)
        if filters:
            for method, args in filters:
                q = getattr(q, method)(*args)
        resp = q.range(offset, offset + 999).execute()
        if not resp.data:
            break
        rows.extend(resp.data)
        if len(resp.data) < 1000:
            break
        offset += 1000
    return rows


# ============================================================
# Step 1: Get all curated restaurants
# ============================================================
resp = sb.table("curated_restaurants").select(
    "venue_id,name,known_slots,service_start,service_end,slot_interval"
).execute()
restaurants = {r["venue_id"]: r for r in resp.data}
print(f"Curated restaurants: {len(restaurants)}")

# Per-venue tracking: times and sources
venue_times = defaultdict(set)
venue_types = defaultdict(set)
venue_sources = defaultdict(lambda: defaultdict(int))  # {vid: {source: count}}


# ============================================================
# Source 1: slot_snapshots (collector / snipe polling)
# ============================================================
print("\n--- Source 1: slot_snapshots ---")
rows = paginated_query("slot_snapshots", "venue_id,slots_json,slot_count",
                       [("gt", ("slot_count", 0))])
print(f"  Rows fetched: {len(rows)}")
src1_venues = set()
for row in rows:
    vid = row["venue_id"]
    sj = row.get("slots_json")
    if not sj or not isinstance(sj, list):
        continue
    for s in sj:
        if isinstance(s, dict):
            t = extract_hhmm(s.get("time", ""))
            if t:
                venue_times[vid].add(t)
                src1_venues.add(vid)
            st = s.get("type", "")
            if st:
                venue_types[vid].add(st.strip())
    venue_sources[vid]["slot_snapshots"] += 1
print(f"  Venues with data: {len(src1_venues)}")


# ============================================================
# Source 2: scout_snapshots (continuous monitoring)
# ============================================================
print("\n--- Source 2: scout_snapshots ---")
rows = paginated_query("scout_snapshots", "venue_id,slots_json,slot_count",
                       [("gt", ("slot_count", 0))])
print(f"  Rows fetched: {len(rows)}")
src2_venues = set()
for row in rows:
    vid = row["venue_id"]
    sj = row.get("slots_json")
    if not sj or not isinstance(sj, list):
        continue
    for s in sj:
        if isinstance(s, dict):
            t = extract_hhmm(s.get("time", ""))
            if t:
                venue_times[vid].add(t)
                src2_venues.add(vid)
            st = s.get("type", "")
            if st:
                venue_types[vid].add(st.strip())
    venue_sources[vid]["scout_snapshots"] += 1
print(f"  Venues with data: {len(src2_venues)}")


# ============================================================
# Source 3: scout_slot_sightings (per-slot lifecycle)
# ============================================================
print("\n--- Source 3: scout_slot_sightings ---")
rows = paginated_query("scout_slot_sightings", "venue_id,slot_time,slot_type")
print(f"  Rows fetched: {len(rows)}")
src3_venues = set()
for row in rows:
    vid = row["venue_id"]
    t = extract_hhmm(row.get("slot_time", ""))
    if t:
        venue_times[vid].add(t)
        src3_venues.add(vid)
    st = row.get("slot_type", "")
    if st:
        venue_types[vid].add(st.strip())
    venue_sources[vid]["scout_slot_sightings"] += 1
print(f"  Venues with data: {len(src3_venues)}")


# ============================================================
# Source 4: drop_observations (drop detection records)
# ============================================================
print("\n--- Source 4: drop_observations ---")
rows = paginated_query("drop_observations",
                       "venue_id,first_slot_time,last_slot_time,slot_types")
print(f"  Rows fetched: {len(rows)}")
src4_venues = set()
for row in rows:
    vid = row["venue_id"]
    for field in ("first_slot_time", "last_slot_time"):
        t = extract_hhmm(row.get(field, ""))
        if t:
            venue_times[vid].add(t)
            src4_venues.add(vid)
    st_arr = row.get("slot_types")
    if st_arr and isinstance(st_arr, list):
        for st in st_arr:
            if st:
                venue_types[vid].add(st.strip())
    venue_sources[vid]["drop_observations"] += 1
print(f"  Venues with data: {len(src4_venues)}")


# ============================================================
# Source 5: scout_events (availability changes)
# ============================================================
print("\n--- Source 5: scout_events ---")
rows = paginated_query("scout_events", "venue_id,slots_added,slots_removed,event_type")
print(f"  Rows fetched: {len(rows)}")
src5_venues = set()
for row in rows:
    vid = row["venue_id"]
    for field in ("slots_added", "slots_removed"):
        arr = row.get(field)
        if not arr or not isinstance(arr, list):
            continue
        for s in arr:
            if isinstance(s, dict):
                t = extract_hhmm(s.get("time", ""))
                if t:
                    venue_times[vid].add(t)
                    src5_venues.add(vid)
                st = s.get("type", "")
                if st:
                    venue_types[vid].add(st.strip())
    venue_sources[vid]["scout_events"] += 1
print(f"  Venues with data: {len(src5_venues)}")


# ============================================================
# Source 6: slot_claims (user preferences — supplementary)
# ============================================================
print("\n--- Source 6: slot_claims ---")
rows = paginated_query("slot_claims", "venue_id,preferred_time")
print(f"  Rows fetched: {len(rows)}")
src6_venues = set()
for row in rows:
    vid = row["venue_id"]
    t = extract_hhmm(row.get("preferred_time", ""))
    if t:
        venue_times[vid].add(t)
        src6_venues.add(vid)
    venue_sources[vid]["slot_claims"] += 1
print(f"  Venues with data: {len(src6_venues)}")


# ============================================================
# Summary of all sources
# ============================================================
all_source_venues = src1_venues | src2_venues | src3_venues | src4_venues | src5_venues | src6_venues
print(f"\n{'='*80}")
print(f"TOTAL: {len(all_source_venues)} venues with data across all 6 sources")
print(f"  slot_snapshots:       {len(src1_venues)} venues")
print(f"  scout_snapshots:      {len(src2_venues)} venues")
print(f"  scout_slot_sightings: {len(src3_venues)} venues")
print(f"  drop_observations:    {len(src4_venues)} venues")
print(f"  scout_events:         {len(src5_venues)} venues")
print(f"  slot_claims:          {len(src6_venues)} venues")


# ============================================================
# Process each restaurant
# ============================================================
print(f"\nVenues with observed slot data: {len(venue_times)}")
print("=" * 80)
print("RESTAURANT-BY-RESTAURANT ANALYSIS")
print("=" * 80)

updates = {}
flags = []

for vid in sorted(venue_times.keys()):
    times = sorted(venue_times[vid])
    types = sorted(venue_types[vid])
    sources = dict(venue_sources[vid])

    r = restaurants.get(vid)
    if not r:
        flags.append(f"WARNING: Venue {vid} has data but NOT in curated_restaurants!")
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
    src_str = ", ".join(f"{k}:{v}" for k, v in sorted(sources.items()))
    print(f"         Sources: {src_str}")
    print(f"         Types: {types}")
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

# Venues with NO data at all
no_data = set(restaurants.keys()) - set(venue_times.keys())
if no_data:
    print()
    print("=" * 80)
    print(f"VENUES WITH ZERO DATA ({len(no_data)}):")
    print("=" * 80)
    for vid in sorted(no_data):
        print(f"  {vid:>6} | {restaurants[vid]['name']}")

# Apply updates
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
