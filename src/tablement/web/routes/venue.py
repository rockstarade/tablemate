"""Venue lookup and search API routes.

Search is stateless (no auth required). Lookup detects booking policy
and returns venue details for the reservation creation form.
Session storage removed — the frontend holds venue state and passes it
when creating a reservation.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from tablement.venue import VenueLookup, parse_resy_url
from tablement.web.ai import detect_policy_with_ai
from tablement.web.schemas import (
    DetectedPolicyOut,
    VenueLookupRequest,
    VenueLookupResponse,
    VenueSearchResult,
    VenueSearchResponse,
)

router = APIRouter()


# ---------- Search (typeahead) ----------


@router.get("/search", response_model=VenueSearchResponse)
async def search_venues(query: str = "", limit: int = 5):
    """Search Resy venues by name. Used for typeahead autocomplete."""
    query = query.strip()
    if len(query) < 2:
        return VenueSearchResponse(results=[])

    venue_lookup = VenueLookup()
    raw = await venue_lookup.search(query, limit=min(limit, 10))

    results = [
        VenueSearchResult(**r)
        for r in raw
        if r.get("resy_id") is not None
    ]
    return VenueSearchResponse(results=results)


# ---------- Select (from search result) ----------


async def _detect_policy(
    venue_lookup: VenueLookup,
    url_slug: str,
    location_slug: str,
    venue_name: str | None,
) -> DetectedPolicyOut | None:
    """Fetch venue content from Resy API and detect booking policy.

    Uses the /3/venue API to get textual content (need_to_know, about, etc.),
    then tries regex patterns first, falling back to Claude AI.
    """
    if not url_slug:
        return None

    try:
        text = await venue_lookup.fetch_venue_content(url_slug, location_slug or None)
        if not text:
            return None

        # Try regex first
        regex_result = venue_lookup.scrape_booking_policy(text)
        if regex_result:
            return DetectedPolicyOut(
                days_ahead=regex_result.days_ahead,
                hour=regex_result.hour,
                minute=regex_result.minute,
                timezone=regex_result.timezone,
                source="regex",
                confidence="high",
            )

        # Fall back to AI
        ai_result = await detect_policy_with_ai(text, venue_name)
        if ai_result.detected and ai_result.days_ahead is not None and ai_result.hour is not None:
            return DetectedPolicyOut(
                days_ahead=ai_result.days_ahead,
                hour=ai_result.hour,
                minute=ai_result.minute or 0,
                timezone=ai_result.timezone,
                source="ai",
                confidence=ai_result.confidence,
                reasoning=ai_result.reasoning,
            )
    except Exception:
        pass  # Policy detection is best-effort

    return None


@router.post("/select", response_model=VenueLookupResponse)
async def select_venue(body: dict):
    """Select a venue from search results and detect policy.

    Stateless — returns venue info + detected policy without storing in session.
    The frontend uses this to populate the reservation form.
    """
    venue_lookup = VenueLookup()
    venue_id = body.get("resy_id")
    venue_name = body.get("name", "")
    url_slug = body.get("url_slug", "")
    location_slug = body.get("location_slug", "")

    if not venue_id:
        raise HTTPException(400, "resy_id is required")

    detected_policy = await _detect_policy(
        venue_lookup, url_slug, location_slug, venue_name,
    )

    return VenueLookupResponse(
        venue_id=venue_id,
        venue_name=venue_name,
        detected_policy=detected_policy,
    )


# ---------- Lookup (by URL) ----------


@router.post("/lookup", response_model=VenueLookupResponse)
async def lookup(body: VenueLookupRequest):
    """Look up a venue from a Resy URL and detect booking policy.

    Stateless — returns venue info + detected policy.
    """
    venue_lookup = VenueLookup()

    url_info = parse_resy_url(body.url)

    try:
        venue_id, venue_name = await venue_lookup.from_url(body.url)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not look up venue: {e}")

    parsed = parse_resy_url(body.url)
    detected_policy = await _detect_policy(
        venue_lookup, parsed["slug"], parsed.get("location", ""), venue_name,
    )

    return VenueLookupResponse(
        venue_id=venue_id,
        venue_name=venue_name,
        detected_policy=detected_policy,
        url_date=url_info["date"],
        url_seats=url_info["seats"],
    )
