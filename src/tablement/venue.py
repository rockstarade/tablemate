"""Venue ID lookup and booking policy scraping from Resy pages."""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx

from tablement.errors import VenueLookupError
from tablement.models import DropTime

logger = logging.getLogger(__name__)

_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Patterns for extracting booking policy from restaurant descriptions
_DAYS_PATTERNS = [
    # "released 30 days in advance"
    re.compile(r"(\d+)\s*days?\s*(?:in\s+advance|ahead|before|out)", re.IGNORECASE),
    # "available 2 weeks in advance"
    re.compile(r"(\d+)\s*weeks?\s*(?:in\s+advance|ahead|before|out)", re.IGNORECASE),
]

_TIME_PATTERNS = [
    # "at 10am", "at 10:00 AM", "at 9 a.m."
    re.compile(
        r"(?:at|@)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)",
        re.IGNORECASE,
    ),
    # "daily at noon"
    re.compile(r"(?:at|@)\s*(noon|midnight)", re.IGNORECASE),
]

# URL pattern: https://resy.com/cities/{location}/venues/{slug}
_RESY_URL_RE = re.compile(
    r"resy\.com/cities/([^/]+)/venues/([^/?#]+)", re.IGNORECASE
)


def _parse_time_match(match: re.Match) -> tuple[int, int] | None:
    """Parse an hour/minute from a regex match. Returns (hour, minute) or None."""
    groups = match.groups()

    # Handle "noon" / "midnight"
    if len(groups) == 1:
        word = groups[0].lower()
        if word == "noon":
            return (12, 0)
        if word == "midnight":
            return (0, 0)
        return None

    hour_str, minute_str, period = groups
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0
    period_lower = period.lower().replace(".", "")

    if period_lower == "pm" and hour != 12:
        hour += 12
    elif period_lower == "am" and hour == 12:
        hour = 0

    return (hour, minute)


def parse_resy_url(url: str) -> dict:
    """Parse a Resy URL for slug, location, date, and seats.

    Returns dict with keys: location, slug, date, seats (any may be None).
    """
    result: dict = {"location": None, "slug": None, "date": None, "seats": None}

    match = _RESY_URL_RE.search(url)
    if match:
        result["location"] = match.group(1)
        result["slug"] = match.group(2)

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "date" in qs:
        result["date"] = qs["date"][0]
    if "seats" in qs:
        try:
            result["seats"] = int(qs["seats"][0])
        except ValueError:
            pass

    return result


class VenueLookup:
    """Resolves venue information from Resy URLs and search."""

    _API_HEADERS = {
        "Authorization": f'ResyAPI api_key="{_API_KEY}"',
        "User-Agent": _UA,
    }

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search Resy venues by name via POST /3/venuesearch/search.

        Filters to NYC area by default using geo coordinates.

        Returns list of dicts with keys:
            resy_id, name, cuisine, neighborhood, locality, region,
            url_slug, location_slug.
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.post(
                "https://api.resy.com/3/venuesearch/search",
                json={
                    "query": query,
                    "per_page": limit,
                    "types": ["venue"],
                    "geo": {"latitude": 40.7128, "longitude": -74.0060},
                },
                headers={
                    **self._API_HEADERS,
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    "Resy search returned %d for query '%s'",
                    resp.status_code, query,
                )
                return []

            data = resp.json()

        hits = data.get("search", {}).get("hits", [])
        results = []
        for h in hits:
            venue_id = h.get("id", {}).get("resy") if isinstance(h.get("id"), dict) else None
            if venue_id is None:
                continue
            cuisine_list = h.get("cuisine", [])
            results.append({
                "resy_id": int(venue_id),
                "name": h.get("name", ""),
                "cuisine": cuisine_list[0] if cuisine_list else "",
                "neighborhood": h.get("neighborhood", ""),
                "locality": h.get("locality", ""),
                "region": h.get("region", ""),
                "url_slug": h.get("url_slug", ""),
                "location_slug": h.get("location", {}).get("url_slug", ""),
            })
        return results

    async def fetch_venue_content(
        self, url_slug: str, location: str | None = None
    ) -> str:
        """Fetch venue description text from the Resy API.

        Combines the 'content' entries (need_to_know, about, why_we_like_it,
        from_the_venue, tagline) and metadata.description into one string
        for policy detection.  Returns empty string on failure.
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            params: dict[str, str] = {"url_slug": url_slug}
            if location:
                params["location"] = location
            resp = await client.get(
                "https://api.resy.com/3/venue",
                params=params,
                headers=self._API_HEADERS,
            )
            if resp.status_code != 200:
                return ""
            data = resp.json()

        parts: list[str] = []

        # Collect all content body entries (need_to_know, about, etc.)
        for entry in data.get("content", []):
            body = entry.get("body")
            if body:
                name = entry.get("name", "")
                parts.append(f"{name}: {body}")

        # metadata.description
        meta_desc = data.get("metadata", {}).get("description", "")
        if meta_desc:
            parts.append(meta_desc)

        return "\n\n".join(parts)

    async def from_url(self, url: str) -> tuple[int, str | None]:
        """
        Extract venue_id and venue name from a Resy restaurant URL.

        Uses the Resy API /3/venue endpoint with the URL slug.
        Returns (venue_id, venue_name).
        """
        parsed = parse_resy_url(url)
        slug = parsed["slug"]
        location = parsed["location"]

        if not slug:
            raise VenueLookupError(
                f"Could not parse venue slug from URL: {url}"
            )

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
        ) as client:
            params = {"url_slug": slug}
            if location:
                params["location"] = location
            resp = await client.get(
                "https://api.resy.com/3/venue",
                params=params,
                headers={
                    "Authorization": f'ResyAPI api_key="{_API_KEY}"',
                    "User-Agent": _UA,
                },
            )
            if resp.status_code != 200:
                raise VenueLookupError(
                    f"Resy API returned {resp.status_code} for slug '{slug}'"
                )
            data = resp.json()

        venue_id = data.get("id", {}).get("resy")
        if venue_id is None:
            raise VenueLookupError(f"No venue_id in API response for '{slug}'")

        venue_name = data.get("name")
        return int(venue_id), venue_name

    def scrape_booking_policy(self, html: str) -> DropTime | None:
        """
        Attempt to extract booking policy from page description text.

        Looks for patterns like:
        - "Reservations are released 30 days in advance at 10am"
        - "Bookings open daily at 9am, 14 days out"

        Returns a DropTime if both days and time are found, None otherwise.
        """
        days_ahead = None
        drop_hour = None
        drop_minute = 0

        # Find days ahead
        for pattern in _DAYS_PATTERNS:
            match = pattern.search(html)
            if match:
                value = int(match.group(1))
                # Check if it was weeks
                if "week" in pattern.pattern:
                    value *= 7
                days_ahead = value
                break

        # Find drop time
        for pattern in _TIME_PATTERNS:
            match = pattern.search(html)
            if match:
                parsed = _parse_time_match(match)
                if parsed:
                    drop_hour, drop_minute = parsed
                    break

        if days_ahead is not None and drop_hour is not None:
            logger.info(
                "Detected booking policy: %d days ahead at %02d:%02d",
                days_ahead, drop_hour, drop_minute,
            )
            return DropTime(
                hour=drop_hour,
                minute=drop_minute,
                days_ahead=days_ahead,
            )

        # Partial match — log what we found
        if days_ahead is not None:
            logger.info("Found days_ahead=%d but no drop time", days_ahead)
        if drop_hour is not None:
            logger.info("Found drop time=%02d:%02d but no days_ahead", drop_hour, drop_minute)

        return None
