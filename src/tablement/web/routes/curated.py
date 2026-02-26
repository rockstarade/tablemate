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

    # Build response with image URL convention
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
            "drop_days_ahead": r.get("drop_days_ahead"),
            "drop_hour": r.get("drop_hour"),
            "drop_minute": r.get("drop_minute", 0),
            "sort_order": r.get("sort_order", 0),
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
            "tagline": row.get("tagline", ""),
            "drop_days_ahead": row.get("drop_days_ahead"),
            "drop_hour": row.get("drop_hour"),
            "drop_minute": row.get("drop_minute", 0),
        }
    }
