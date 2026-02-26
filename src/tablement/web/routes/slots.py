"""Slot availability API — fetch Resy slots with conflict annotation.

Requires auth. Calls Resy's find_slots() in scout mode (no user token)
and annotates each slot with whether another Tablement user has claimed it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from tablement.api import ResyApiClient
from tablement.web import db
from tablement.web.deps import get_user_id

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/available")
async def get_available_slots(
    venue_id: int = Query(...),
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    party_size: int = Query(default=2, ge=1, le=20),
    user_id: str = Depends(get_user_id),
):
    """Fetch available slots for a venue+date with conflict annotation.

    1. Calls Resy find_slots() in scout mode (no auth token needed)
    2. Fetches active slot_claims for this venue+date from Tablement DB
    3. Annotates each slot: claimed true/false (if another user is targeting it)
    4. Returns annotated slot list grouped by seating type
    """
    try:
        async with ResyApiClient(scout=True) as client:
            slots = await client.find_slots(venue_id, date, party_size)
    except Exception as e:
        logger.warning("find_slots failed for venue %d: %s", venue_id, e)
        raise HTTPException(502, f"Could not fetch availability: {e}")

    # Get active claims from other Tablement users
    try:
        claims = await db.get_active_claims(venue_id, date)
    except Exception:
        claims = []

    # Build claimed set: time+type combos that other users are targeting
    claimed_set = set()
    for c in claims:
        if c.get("user_id") != user_id:
            key = f"{c.get('target_time', '')}|{c.get('seating_type', '')}"
            claimed_set.add(key)

    # Build annotated slot list grouped by seating type
    slot_groups: dict[str, list] = {}
    for slot in slots:
        slot_time = slot.date.start if hasattr(slot, "date") else ""
        seating_type = slot.config.type if hasattr(slot, "config") else "Dining Room"
        config_token = slot.config.token if hasattr(slot, "config") else ""

        claim_key = f"{slot_time}|{seating_type}"
        is_claimed = claim_key in claimed_set

        slot_data = {
            "time": slot_time,
            "type": seating_type,
            "config_token": config_token,
            "claimed": is_claimed,
        }

        if seating_type not in slot_groups:
            slot_groups[seating_type] = []
        slot_groups[seating_type].append(slot_data)

    # Sort each group by time
    for group in slot_groups.values():
        group.sort(key=lambda s: s["time"])

    return {
        "venue_id": venue_id,
        "date": date,
        "party_size": party_size,
        "total_slots": len(slots),
        "slot_groups": slot_groups,
    }
