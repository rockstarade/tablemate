"""Stripe payment routes — setup intents, payment method management.

Flow:
1. POST /setup-intent → creates Stripe SetupIntent + customer
2. POST /methods      → saves a payment method after Stripe.js confirmation
3. GET  /methods      → lists saved payment methods
4. DELETE /methods/:id → removes a payment method
"""

from __future__ import annotations

import logging
import os

import stripe
from fastapi import APIRouter, Depends, HTTPException

from tablement.web import db
from tablement.web.deps import get_user_id
from tablement.web.schemas import (
    PaymentMethodListResponse,
    PaymentMethodOut,
    SavePaymentMethodRequest,
    SetupIntentResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Initialize Stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")


@router.post("/setup-intent", response_model=SetupIntentResponse)
async def create_setup_intent(user_id: str = Depends(get_user_id)):
    """Create a Stripe SetupIntent for the frontend to collect card details.

    If the user doesn't have a Stripe customer yet, creates one.
    Returns the client_secret for Stripe.js to confirm the SetupIntent.
    """
    if not stripe.api_key:
        raise HTTPException(500, "Stripe not configured")

    profile = await db.get_profile(user_id)
    if not profile:
        raise HTTPException(400, "Profile not found")

    # Get or create Stripe customer
    customer_id = profile.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(
            metadata={"tablement_user_id": user_id},
        )
        customer_id = customer.id
        await db.upsert_profile(user_id, stripe_customer_id=customer_id)
        logger.info("Created Stripe customer %s for user %s", customer_id, user_id)

    # Create SetupIntent
    setup_intent = stripe.SetupIntent.create(
        customer=customer_id,
        payment_method_types=["card"],
    )

    return SetupIntentResponse(
        client_secret=setup_intent.client_secret,
        stripe_customer_id=customer_id,
    )


@router.post("/methods", response_model=PaymentMethodOut)
async def save_payment_method(
    body: SavePaymentMethodRequest,
    user_id: str = Depends(get_user_id),
):
    """Save a payment method after Stripe.js has confirmed the SetupIntent.

    The frontend sends the stripe_payment_method_id after confirming.
    We fetch the payment method details from Stripe and save to our DB.
    """
    if not stripe.api_key:
        raise HTTPException(500, "Stripe not configured")

    # Fetch payment method details from Stripe
    try:
        pm = stripe.PaymentMethod.retrieve(body.stripe_payment_method_id)
    except Exception as e:
        raise HTTPException(400, f"Invalid payment method: {e}")

    card = pm.get("card", {})
    brand = card.get("brand", "unknown")
    last_four = card.get("last4", "????")

    # Check if this is the user's first payment method (make it default)
    existing = await db.list_payment_methods(user_id)
    is_default = len(existing) == 0

    row = await db.create_payment_method(
        user_id=user_id,
        stripe_payment_method_id=body.stripe_payment_method_id,
        brand=brand,
        last_four=last_four,
        is_default=is_default,
    )

    return PaymentMethodOut(
        id=row["id"],
        brand=brand,
        last_four=last_four,
        is_default=is_default,
    )


@router.get("/methods", response_model=PaymentMethodListResponse)
async def list_methods(user_id: str = Depends(get_user_id)):
    """List saved payment methods."""
    rows = await db.list_payment_methods(user_id)
    methods = [
        PaymentMethodOut(
            id=r["id"],
            brand=r.get("brand"),
            last_four=r.get("last_four"),
            is_default=r.get("is_default", False),
        )
        for r in rows
    ]
    return PaymentMethodListResponse(methods=methods)


@router.delete("/methods/{pm_id}")
async def delete_method(pm_id: str, user_id: str = Depends(get_user_id)):
    """Remove a saved payment method."""
    await db.delete_payment_method(pm_id, user_id)
    return {"deleted": True}
