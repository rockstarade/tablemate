"""Curated restaurant API routes — public browse grid data.

Serves the list of hand-picked, hardest-to-get NYC restaurants for
the consumer-facing browse page. No auth required for reading.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from tablement.web import db

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
        restaurants.append({
            "venue_id": r["venue_id"],
            "name": r["name"],
            "cuisine": r.get("cuisine", ""),
            "neighborhood": r.get("neighborhood", ""),
            "url_slug": r.get("url_slug", ""),
            "image_url": r.get("image_url") or f"/static/restaurants/{r['venue_id']}.jpg",
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
        })

    return {"restaurants": restaurants}


@router.get("/{venue_id}")
async def get_curated(venue_id: int):
    """Get a single curated restaurant's details."""
    row = await db.get_curated_restaurant(venue_id)
    if not row:
        return {"restaurant": None}

    return {
        "restaurant": {
            "venue_id": row["venue_id"],
            "name": row["name"],
            "cuisine": row.get("cuisine", ""),
            "neighborhood": row.get("neighborhood", ""),
            "url_slug": row.get("url_slug", ""),
            "image_url": row.get("image_url") or f"/static/restaurants/{row['venue_id']}.jpg",
            "slot_interval": row.get("slot_interval", 15),
            "drop_days_ahead": row.get("drop_days_ahead"),
            "drop_hour": row.get("drop_hour"),
            "drop_minute": row.get("drop_minute", 0),
            "service_start": row.get("service_start", "17:00"),
            "service_end": row.get("service_end", "22:00"),
            "hot_start": row.get("hot_start", "19:00"),
            "hot_end": row.get("hot_end", "20:30"),
        }
    }
