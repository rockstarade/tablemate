"""Claude AI booking policy detection."""

from __future__ import annotations

import json
import logging
import os
import re

from tablement.web.schemas import PolicyDetectResponse

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You extract restaurant reservation booking policies from text.

Given a restaurant's page content, find:
1. How many days in advance reservations are released (days_ahead)
2. What time of day new reservations appear (hour in 24h format, minute)
3. Timezone (default America/New_York if not stated)

Respond ONLY with JSON, no other text:
{"detected": true, "days_ahead": 30, "hour": 10, "minute": 0, "timezone": "America/New_York", "confidence": "high", "reasoning": "Found: reservations released 30 days in advance at 10am"}

If you cannot find booking policy info, respond:
{"detected": false, "reasoning": "No booking policy information found in text"}
"""


async def detect_policy_with_ai(
    text: str,
    venue_name: str | None = None,
) -> PolicyDetectResponse:
    """Call Claude Haiku to extract booking policy from restaurant page text."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return PolicyDetectResponse(
            detected=False,
            reasoning="AI detection unavailable (ANTHROPIC_API_KEY not set)",
        )

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)

        # Strip HTML, collapse whitespace, truncate
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()[:4000]

        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Restaurant: {venue_name or 'Unknown'}\n\n{clean}",
            }],
        )

        response_text = message.content[0].text
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            return PolicyDetectResponse(
                detected=data.get("detected", False),
                days_ahead=data.get("days_ahead"),
                hour=data.get("hour"),
                minute=data.get("minute"),
                timezone=data.get("timezone", "America/New_York"),
                confidence=data.get("confidence", "low"),
                reasoning=data.get("reasoning", ""),
            )

        return PolicyDetectResponse(
            detected=False,
            reasoning="Could not parse AI response",
        )

    except Exception as e:
        logger.warning("AI policy detection failed: %s", e)
        return PolicyDetectResponse(
            detected=False,
            reasoning=f"AI error: {e}",
        )
