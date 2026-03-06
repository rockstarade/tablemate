#!/usr/bin/env python3
"""Resolve placeholder Resy venue IDs (99xxx) to real ones via the Resy API.

Reads the list of restaurants from migration 014 that have placeholder venue_ids
(99xxx), looks up each one by url_slug using the Resy /3/venue endpoint, and
outputs SQL UPDATE statements to fix the database.

Usage:
    python scripts/resolve_venue_ids.py
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx

# ---------------------------------------------------------------------------
# Resy API config (matches src/tablement/venue.py)
# ---------------------------------------------------------------------------
API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"
API_HEADERS = {
    "Authorization": f'ResyAPI api_key="{API_KEY}"',
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# ---------------------------------------------------------------------------
# All restaurants with placeholder venue_ids (99xxx) from migration 014
# Each entry: (placeholder_venue_id, name, url_slug, <rest of INSERT values>)
# ---------------------------------------------------------------------------
PLACEHOLDER_RESTAURANTS = [
    # PART 2: Hardest to Book + Speakeasies
    {
        "placeholder_id": 99910,
        "name": "PDT",
        "url_slug": "pdt",
        "cuisine": "Speakeasy",
        "neighborhood": "East Village",
        "drop_days_ahead": 7,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "02:00",
        "hot_start": "20:00",
        "hot_end": "00:00",
        "slot_interval": 30,
        "sort_order": 51,
        "tagline": "Enter through a phone booth in a hot dog shop",
    },
    {
        "placeholder_id": 99911,
        "name": "Raines Law Room",
        "url_slug": "raines-law-room",
        "cuisine": "Speakeasy",
        "neighborhood": "Chelsea",
        "drop_days_ahead": 14,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "02:00",
        "hot_start": "20:00",
        "hot_end": "23:00",
        "slot_interval": 30,
        "sort_order": 53,
        "tagline": "1920s cocktail parlor with velvet couches and bell service",
    },
    {
        "placeholder_id": 99912,
        "name": "Bathtub Gin",
        "url_slug": "bathtub-gin",
        "cuisine": "Speakeasy",
        "neighborhood": "Chelsea",
        "drop_days_ahead": 14,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "18:00",
        "service_end": "02:00",
        "hot_start": "20:00",
        "hot_end": "23:00",
        "slot_interval": 30,
        "sort_order": 54,
        "tagline": "Prohibition-era speakeasy hidden behind a coffee shop",
    },
    {
        "placeholder_id": 99913,
        "name": "Nothing Really Matters",
        "url_slug": "nothing-really-matters",
        "cuisine": "Cocktail Bar",
        "neighborhood": "Midtown",
        "drop_days_ahead": 14,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "02:00",
        "hot_start": "20:00",
        "hot_end": "23:00",
        "slot_interval": 30,
        "sort_order": 55,
        "tagline": "Subterranean cocktails down a set of subway stairs",
    },
    {
        "placeholder_id": 99914,
        "name": "Misi",
        "url_slug": "misi",
        "cuisine": "Italian",
        "neighborhood": "Williamsburg",
        "drop_days_ahead": 30,
        "drop_hour": 0,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:30",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 56,
        "tagline": "Waterfront pasta from the Lilia team",
    },
    {
        "placeholder_id": 99915,
        "name": "Rubirosa",
        "url_slug": "rubirosa",
        "cuisine": "Italian",
        "neighborhood": "Nolita",
        "drop_days_ahead": 7,
        "drop_hour": 0,
        "drop_minute": 0,
        "service_start": "11:30",
        "service_end": "23:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 57,
        "tagline": "Thin-crust pizza and red-sauce classics in Nolita",
    },
    {
        "placeholder_id": 99916,
        "name": "Golden Diner",
        "url_slug": "golden-diner",
        "cuisine": "American-Asian",
        "neighborhood": "Chinatown",
        "drop_days_ahead": 30,
        "drop_hour": 0,
        "drop_minute": 0,
        "service_start": "10:00",
        "service_end": "15:00",
        "hot_start": "11:00",
        "hot_end": "13:00",
        "slot_interval": 15,
        "sort_order": 58,
        "tagline": "All-day diner with Asian-American comfort food",
    },
    {
        "placeholder_id": 99917,
        "name": "Bangkok Supper Club",
        "url_slug": "bangkok-supper-club",
        "cuisine": "Thai",
        "neighborhood": "Meatpacking",
        "drop_days_ahead": 30,
        "drop_hour": 0,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "23:00",
        "hot_start": "19:00",
        "hot_end": "21:00",
        "slot_interval": 15,
        "sort_order": 59,
        "tagline": "Thai fine dining in the Meatpacking District",
    },
    {
        "placeholder_id": 99918,
        "name": "Kisa",
        "url_slug": "kisa",
        "cuisine": "Japanese",
        "neighborhood": "LES",
        "drop_days_ahead": 15,
        "drop_hour": 0,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 60,
        "tagline": "Intimate Japanese omakase on the Lower East Side",
    },
    {
        "placeholder_id": 99919,
        "name": "Bridges",
        "url_slug": "bridges",
        "cuisine": "Chinese",
        "neighborhood": "Chinatown",
        "drop_days_ahead": 21,
        "drop_hour": 12,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 61,
        "tagline": "Modern Chinese cuisine in Chinatown",
    },
    {
        "placeholder_id": 99920,
        "name": "Theodora",
        "url_slug": "theodora",
        "cuisine": "Mediterranean",
        "neighborhood": "Fort Greene",
        "drop_days_ahead": 30,
        "drop_hour": 9,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 62,
        "tagline": "Mediterranean dining in Fort Greene, Brooklyn",
    },
    {
        "placeholder_id": 99921,
        "name": "Penny",
        "url_slug": "penny",
        "cuisine": "Italian",
        "neighborhood": "East Village",
        "drop_days_ahead": 14,
        "drop_hour": 9,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:30",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 63,
        "tagline": "Neighborhood Italian in the East Village",
    },
    {
        "placeholder_id": 99922,
        "name": "Bong",
        "url_slug": "bong",
        "cuisine": "Korean",
        "neighborhood": "Crown Heights",
        "drop_days_ahead": 20,
        "drop_hour": 0,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 64,
        "tagline": "Korean fine dining in Crown Heights",
    },
    {
        "placeholder_id": 99923,
        "name": "Le Chene",
        "url_slug": "le-chene",
        "cuisine": "French",
        "neighborhood": "West Village",
        "drop_days_ahead": 14,
        "drop_hour": 9,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:30",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 65,
        "tagline": "French bistro in the West Village",
    },
    {
        "placeholder_id": 99924,
        "name": "Ha's Snack Bar",
        "url_slug": "has-snack-bar",
        "cuisine": "Vietnamese",
        "neighborhood": "LES",
        "drop_days_ahead": 21,
        "drop_hour": 12,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 66,
        "tagline": "Vietnamese small plates on the Lower East Side",
    },
    {
        "placeholder_id": 99925,
        "name": "Lei",
        "url_slug": "lei",
        "cuisine": "Chinese",
        "neighborhood": "Chinatown",
        "drop_days_ahead": 14,
        "drop_hour": 9,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 67,
        "tagline": "Modern Chinese in Chinatown",
    },
    {
        "placeholder_id": 99926,
        "name": "Borgo",
        "url_slug": "borgo",
        "cuisine": "Italian",
        "neighborhood": "Flatiron",
        "drop_days_ahead": 21,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:30",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 68,
        "tagline": "Contemporary Italian in the Flatiron District",
    },
    {
        "placeholder_id": 99927,
        "name": "Ramen By Ra",
        "url_slug": "ramen-by-ra",
        "cuisine": "Japanese Ramen",
        "neighborhood": "East Village",
        "drop_days_ahead": 15,
        "drop_hour": 9,
        "drop_minute": 0,
        "service_start": "11:30",
        "service_end": "22:00",
        "hot_start": "18:00",
        "hot_end": "20:00",
        "slot_interval": 30,
        "sort_order": 69,
        "tagline": "Cult-following ramen with twice-monthly drops",
    },
    # PART 3: Michelin-starred restaurants
    {
        "placeholder_id": 99930,
        "name": "Chef's Table at Brooklyn Fare",
        "url_slug": "chefs-table-at-brooklyn-fare",
        "cuisine": "French-Japanese",
        "neighborhood": "Hell's Kitchen",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "18:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 70,
        "tagline": "Intimate counter for French-Japanese omakase",
    },
    {
        "placeholder_id": 99931,
        "name": "Cesar",
        "url_slug": "cesar",
        "cuisine": "French-Japanese Seafood",
        "neighborhood": "Hudson Square",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 71,
        "tagline": "Refined seafood tasting menu in Hudson Square",
    },
    {
        "placeholder_id": 99932,
        "name": "Gabriel Kreuther",
        "url_slug": "gabriel-kreuther",
        "cuisine": "French",
        "neighborhood": "Bryant Park",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "12:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "21:00",
        "slot_interval": 30,
        "sort_order": 72,
        "tagline": "Alsatian-inspired French cuisine near Bryant Park",
    },
    {
        "placeholder_id": 99933,
        "name": "Jean-Georges",
        "url_slug": "jean-georges",
        "cuisine": "French-Asian",
        "neighborhood": "Upper West Side",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "12:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 73,
        "tagline": "Vongerichten flagship French-Asian fine dining",
    },
    {
        "placeholder_id": 99934,
        "name": "The Modern",
        "url_slug": "the-modern",
        "cuisine": "Contemporary American",
        "neighborhood": "Midtown",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "12:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 75,
        "tagline": "Refined dining overlooking MoMA sculpture garden",
    },
    {
        "placeholder_id": 99935,
        "name": "Saga",
        "url_slug": "saga-ny",
        "cuisine": "New American",
        "neighborhood": "Financial District",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 76,
        "tagline": "Sky-high tasting menu with 360-degree views",
    },
    {
        "placeholder_id": 99936,
        "name": "Restaurant Daniel",
        "url_slug": "daniel",
        "cuisine": "French",
        "neighborhood": "Upper East Side",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 78,
        "tagline": "Daniel Boulud grand French flagship",
    },
    {
        "placeholder_id": 99937,
        "name": "Cafe Boulud",
        "url_slug": "cafe-boulud",
        "cuisine": "French",
        "neighborhood": "Upper East Side",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "12:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 79,
        "tagline": "Daniel Boulud beloved French brasserie",
    },
    {
        "placeholder_id": 99938,
        "name": "Dirt Candy",
        "url_slug": "dirt-candy",
        "cuisine": "Vegetable-Forward",
        "neighborhood": "LES",
        "drop_days_ahead": 14,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 81,
        "tagline": "Pioneering vegetable-forward tasting menu",
    },
    {
        "placeholder_id": 99939,
        "name": "Gramercy Tavern",
        "url_slug": "gramercy-tavern",
        "cuisine": "American",
        "neighborhood": "Flatiron",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "12:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 83,
        "tagline": "Seasonal American fine dining by Danny Meyer",
    },
    {
        "placeholder_id": 99940,
        "name": "Huso",
        "url_slug": "huso-nyc",
        "cuisine": "Contemporary Seafood",
        "neighborhood": "Tribeca",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 84,
        "tagline": "Top Chef winner caviar-fueled tasting menu",
    },
    {
        "placeholder_id": 99941,
        "name": "Joji",
        "url_slug": "joji",
        "cuisine": "Japanese Omakase",
        "neighborhood": "Midtown East",
        "drop_days_ahead": 14,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "18:30",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 86,
        "tagline": "Hidden omakase below One Vanderbilt",
    },
    {
        "placeholder_id": 99942,
        "name": "l'abeille",
        "url_slug": "labielle",
        "cuisine": "French-Japanese",
        "neighborhood": "Tribeca",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 88,
        "tagline": "French-Japanese harmony on Tribeca cobblestones",
    },
    {
        "placeholder_id": 99943,
        "name": "Le Pavillon",
        "url_slug": "le-pavillon",
        "cuisine": "French Seafood",
        "neighborhood": "Midtown East",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "12:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 89,
        "tagline": "Boulud seafood showcase in One Vanderbilt",
    },
    {
        "placeholder_id": 99944,
        "name": "Meju",
        "url_slug": "meju",
        "cuisine": "Korean",
        "neighborhood": "Long Island City",
        "drop_days_ahead": 14,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "18:30",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 91,
        "tagline": "Eight-seat Korean fermentation counter",
    },
    {
        "placeholder_id": 99945,
        "name": "Noksu",
        "url_slug": "noksu",
        "cuisine": "Contemporary Korean",
        "neighborhood": "Koreatown",
        "drop_days_ahead": 14,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "18:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 92,
        "tagline": "Hidden Korean omakase inside a subway station",
    },
    {
        "placeholder_id": 99946,
        "name": "Oiji Mi",
        "url_slug": "oiji-mi",
        "cuisine": "Korean",
        "neighborhood": "Flatiron",
        "drop_days_ahead": 14,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 93,
        "tagline": "Contemporary Korean five-course in Flatiron",
    },
    {
        "placeholder_id": 99947,
        "name": "Shmone",
        "url_slug": "shmone",
        "cuisine": "Mediterranean",
        "neighborhood": "Greenwich Village",
        "drop_days_ahead": 14,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 15,
        "sort_order": 96,
        "tagline": "Intimate Mediterranean gem in the Village",
    },
    {
        "placeholder_id": 99948,
        "name": "Shota Omakase",
        "url_slug": "shota-omakase",
        "cuisine": "Japanese Sushi",
        "neighborhood": "Williamsburg",
        "drop_days_ahead": 14,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:30",
        "service_end": "22:00",
        "hot_start": "18:30",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 97,
        "tagline": "Intimate sushi counter in Williamsburg",
    },
    {
        "placeholder_id": 99949,
        "name": "Sushi Nakazawa",
        "url_slug": "sushi-nakazawa",
        "cuisine": "Japanese Sushi",
        "neighborhood": "West Village",
        "drop_days_ahead": 30,
        "drop_hour": 10,
        "drop_minute": 0,
        "service_start": "17:00",
        "service_end": "22:00",
        "hot_start": "19:00",
        "hot_end": "20:30",
        "slot_interval": 30,
        "sort_order": 99,
        "tagline": "Jiro Ono protege celebrated sushi counter",
    },
]

# Delay between API requests (seconds) to avoid rate limiting
REQUEST_DELAY = 1.0


async def lookup_venue(
    client: httpx.AsyncClient,
    url_slug: str,
    location: str | None = None,
) -> dict | None:
    """Look up a venue by url_slug via GET /3/venue.

    Returns the full API response dict on success, None on failure.
    """
    params: dict[str, str] = {"url_slug": url_slug}
    if location:
        params["location"] = location

    try:
        resp = await client.get(
            "https://api.resy.com/3/venue",
            params=params,
            headers=API_HEADERS,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            print(f"  [WARN] HTTP {resp.status_code} for slug '{url_slug}'")
            return None
        return resp.json()
    except Exception as e:
        print(f"  [ERROR] Request failed for slug '{url_slug}': {e}")
        return None


def sql_escape(s: str) -> str:
    """Escape single quotes for SQL strings."""
    return s.replace("'", "''")


async def main() -> None:
    resolved: list[dict] = []
    failed: list[dict] = []

    print("=" * 70)
    print("Resolving placeholder venue IDs via Resy API")
    print(f"Total restaurants to resolve: {len(PLACEHOLDER_RESTAURANTS)}")
    print("=" * 70)
    print()

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        for i, restaurant in enumerate(PLACEHOLDER_RESTAURANTS):
            slug = restaurant["url_slug"]
            name = restaurant["name"]
            placeholder = restaurant["placeholder_id"]

            print(f"[{i + 1}/{len(PLACEHOLDER_RESTAURANTS)}] {name} (slug: {slug}) ... ", end="", flush=True)

            data = await lookup_venue(client, slug)

            if data is None:
                # Try with NYC location as fallback
                data = await lookup_venue(client, slug, location="ny")

            if data and data.get("id", {}).get("resy"):
                real_id = int(data["id"]["resy"])
                venue_name = data.get("name", name)
                location_info = data.get("location", {})
                neighborhood = location_info.get("neighborhood", restaurant["neighborhood"])

                resolved.append({
                    **restaurant,
                    "real_id": real_id,
                    "api_name": venue_name,
                    "api_neighborhood": neighborhood,
                })
                print(f"OK -> venue_id = {real_id} ({venue_name})")
            else:
                failed.append(restaurant)
                print("FAILED (not found on Resy)")

            # Rate limit delay (skip after last request)
            if i < len(PLACEHOLDER_RESTAURANTS) - 1:
                await asyncio.sleep(REQUEST_DELAY)

    # -----------------------------------------------------------------------
    # Print results summary
    # -----------------------------------------------------------------------
    print()
    print("=" * 70)
    print(f"RESULTS: {len(resolved)} resolved, {len(failed)} failed")
    print("=" * 70)

    if failed:
        print()
        print("FAILED LOOKUPS (slug may be wrong or venue not on Resy):")
        for r in failed:
            print(f"  - {r['name']} (slug: {r['url_slug']}, placeholder: {r['placeholder_id']})")

    # -----------------------------------------------------------------------
    # Output 1: SQL UPDATE statements to fix existing rows in the database
    # -----------------------------------------------------------------------
    if resolved:
        print()
        print("=" * 70)
        print("SQL UPDATE STATEMENTS (fix existing rows)")
        print("=" * 70)
        print()
        for r in resolved:
            print(f"-- {r['name']}: {r['placeholder_id']} -> {r['real_id']}")
            print(f"UPDATE curated_restaurants SET venue_id = {r['real_id']}")
            print(f"WHERE venue_id = {r['placeholder_id']};")
            print()

    # -----------------------------------------------------------------------
    # Output 2: Updated INSERT statements for a corrected migration
    # -----------------------------------------------------------------------
    print()
    print("=" * 70)
    print("CORRECTED INSERT STATEMENTS FOR MIGRATION 014")
    print("(Replace the placeholder entries with these)")
    print("=" * 70)
    print()
    print("INSERT INTO curated_restaurants (")
    print("    venue_id, name, cuisine, neighborhood, url_slug,")
    print("    drop_days_ahead, drop_hour, drop_minute,")
    print("    service_start, service_end, hot_start, hot_end,")
    print("    slot_interval, sort_order, is_active, tagline")
    print(") VALUES")

    # Combine resolved + failed (keep placeholder for failed ones)
    all_restaurants = []
    resolved_map = {r["placeholder_id"]: r for r in resolved}

    for restaurant in PLACEHOLDER_RESTAURANTS:
        pid = restaurant["placeholder_id"]
        if pid in resolved_map:
            r = resolved_map[pid]
            all_restaurants.append({
                "venue_id": r["real_id"],
                **{k: v for k, v in restaurant.items() if k != "placeholder_id"},
            })
        else:
            # Keep placeholder for unresolved
            all_restaurants.append({
                "venue_id": restaurant["placeholder_id"],
                **{k: v for k, v in restaurant.items() if k != "placeholder_id"},
            })

    for i, r in enumerate(all_restaurants):
        is_last = i == len(all_restaurants) - 1
        comma = "" if is_last else ","

        name_escaped = sql_escape(r["name"])
        cuisine_escaped = sql_escape(r["cuisine"])
        neighborhood_escaped = sql_escape(r["neighborhood"])
        tagline_escaped = sql_escape(r["tagline"])

        # Check if this was unresolved
        was_placeholder = r["venue_id"] >= 99000
        marker = "  -- PLACEHOLDER (not resolved)" if was_placeholder else ""

        print(f"    ({r['venue_id']}, '{name_escaped}', '{cuisine_escaped}', "
              f"'{neighborhood_escaped}', '{r['url_slug']}',")
        print(f"     {r['drop_days_ahead']}, {r['drop_hour']}, {r['drop_minute']}, "
              f"'{r['service_start']}', '{r['service_end']}', '{r['hot_start']}', '{r['hot_end']}',")
        print(f"     {r['slot_interval']}, {r['sort_order']}, true,")
        print(f"     '{tagline_escaped}'){comma}{marker}")
        print()

    print("ON CONFLICT (venue_id) DO NOTHING;")


if __name__ == "__main__":
    asyncio.run(main())
