"""Curated restaurant API routes — public browse grid data.

Serves the list of hand-picked, hardest-to-get restaurants for
the consumer-facing browse page. No auth required for reading.
Supports both Resy and OpenTable platforms.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from tablement.web import db

# Scout-observed time slots per venue. Updated as scouts run.
# Once the known_slots DB column exists, this falls back to DB data.
# Format: venue_id → list of "HH:MM" strings
KNOWN_SLOTS: dict[int, list[str]] = {
    # Semma: observed 2026-03-06 drop for 2026-03-21
    # 3 clusters at 15-min intervals: early (5pm), prime (7pm), late (9pm)
    1263: ["17:00", "17:15", "17:30", "19:00", "19:15", "19:30", "21:00", "21:15", "21:30"],
}

# ── Platform & city mappings ──────────────────────────────────────
# Stored in code until DB columns are added via Supabase SQL editor.
# OpenTable restaurants use venue_ids in the 80xxx range.
OPENTABLE_VENUE_IDS: set[int] = {
    80001,  # Don Angie
    80002,  # Soothr
    80003,  # Una Pizza Napoletana
    80004,  # Le Veau d'Or
    80005,  # San Sabino
    80006,  # Estela
    80007,  # Altro Paradiso
}

# venue_id → city slug (everything not listed defaults to "nyc")
CITY_MAP: dict[int, str] = {
    73777: "boston",  # Tonino
}

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/")
async def list_curated():
    """Public list of active curated restaurants for the browse grid.

    Returns restaurants ordered by sort_order. No auth required —
    this is public data visible to anyone on the browse page.
    """
    rows = await db.list_curated_restaurants(active_only=True)

    # Build response with image URL convention + service hours
    restaurants = []
    for r in rows:
        vid = r["venue_id"]
        platform = "opentable" if vid in OPENTABLE_VENUE_IDS else "resy"
        city = CITY_MAP.get(vid, "nyc")
        restaurants.append({
            "venue_id": vid,
            "name": r["name"],
            "cuisine": r.get("cuisine", ""),
            "neighborhood": r.get("neighborhood", ""),
            "url_slug": r.get("url_slug", ""),
            "image_url": r.get("image_url") or f"/static/restaurants/{vid}.jpg",
            "tagline": r.get("tagline", ""),
            "slot_interval": r.get("slot_interval", 15),
            "drop_days_ahead": r.get("drop_days_ahead"),
            "drop_hour": r.get("drop_hour"),
            "drop_minute": r.get("drop_minute", 0),
            "sort_order": r.get("sort_order", 0),
            "service_start": r.get("service_start", "17:00"),
            "service_end": r.get("service_end", "22:00"),
            "hot_start": r.get("hot_start", "19:00"),
            "hot_end": r.get("hot_end", "20:30"),
            "is_hot": r.get("is_hot", False),
            "known_slots": r.get("known_slots") or KNOWN_SLOTS.get(vid),
            "platform": platform,
            "city": city,
        })

    return {"restaurants": restaurants}


@router.get("/{venue_id}")
async def get_curated(venue_id: int):
    """Get a single curated restaurant's details."""
    row = await db.get_curated_restaurant(venue_id)
    if not row:
        return {"restaurant": None}

    vid = row["venue_id"]
    platform = "opentable" if vid in OPENTABLE_VENUE_IDS else "resy"
    city = CITY_MAP.get(vid, "nyc")
    return {
        "restaurant": {
            "venue_id": vid,
            "name": row["name"],
            "cuisine": row.get("cuisine", ""),
            "neighborhood": row.get("neighborhood", ""),
            "url_slug": row.get("url_slug", ""),
            "image_url": row.get("image_url") or f"/static/restaurants/{vid}.jpg",
            "slot_interval": row.get("slot_interval", 15),
            "drop_days_ahead": row.get("drop_days_ahead"),
            "drop_hour": row.get("drop_hour"),
            "drop_minute": row.get("drop_minute", 0),
            "service_start": row.get("service_start", "17:00"),
            "service_end": row.get("service_end", "22:00"),
            "hot_start": row.get("hot_start", "19:00"),
            "hot_end": row.get("hot_end", "20:30"),
            "known_slots": row.get("known_slots") or KNOWN_SLOTS.get(vid),
            "platform": platform,
            "city": city,
        }
    }
