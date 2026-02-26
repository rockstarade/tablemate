"""Standalone AI policy detection route."""

from __future__ import annotations

from fastapi import APIRouter

from tablement.web.ai import detect_policy_with_ai
from tablement.web.schemas import PolicyDetectRequest, PolicyDetectResponse

router = APIRouter()


@router.post("/detect", response_model=PolicyDetectResponse)
async def detect(body: PolicyDetectRequest):
    return await detect_policy_with_ai(body.description_text, body.venue_name)
