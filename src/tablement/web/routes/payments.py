"""Stripe payment routes — setup intents, payment method management, credits.

Flow:
1. POST /setup-intent → creates Stripe SetupIntent + customer
2. POST /methods      → saves a payment method after Stripe.js confirmation
3. GET  /methods      → lists saved payment methods
4. DELETE /methods/:id → removes a payment method
5. GET  /credits      → check credit balance
6. POST /buy-credits  → purchase a credit pack ($12 single or $50 five-pack)
7. GET  /transactions → billing history

Charge-on-success:
  charge_for_booking() is called from the snipe loop when a reservation
  is confirmed. It deducts a credit if available, otherwise charges $12
  to the user's saved card.
"""

from __future__ import annotations

import logging
import os

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request

from tablement.web import db
from tablement.web.deps import get_user_id
from tablement.web.ratelimit import limiter
from tablement.web.schemas import (
    BuyCreditsRequest,
    CreditBalanceResponse,
    PaymentMethodListResponse,
    PaymentMethodOut,
    SavePaymentMethodRequest,
    SetupIntentResponse,
    TransactionListResponse,
    TransactionOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Initialize Stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

# Pricing (in cents)
SINGLE_RESERVATION_CENTS = 1200   # $12
FIVE_PACK_CENTS = 5000            # $50
FIVE_PACK_CREDITS = 5


# ---------------------------------------------------------------------------
# Setup Intent + Payment Methods (existing)
# ---------------------------------------------------------------------------


@router.post("/setup-intent", response_model=SetupIntentResponse)
@limiter.limit("10/minute")
async def create_setup_intent(request: Request, user_id: str = Depends(get_user_id)):
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
        logger.error("Stripe PaymentMethod retrieve failed: %s", e)
        raise HTTPException(400, "Invalid payment method. Please try again.")

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


# ---------------------------------------------------------------------------
# Credits
# ---------------------------------------------------------------------------


@router.get("/credits", response_model=CreditBalanceResponse)
async def get_credits(user_id: str = Depends(get_user_id)):
    """Return the user's current credit balance."""
    credits = await db.get_user_credits(user_id)
    return CreditBalanceResponse(credits=credits)


@router.post("/buy-credits")
@limiter.limit("5/minute")
async def buy_credits(
    request: Request,
    body: BuyCreditsRequest,
    user_id: str = Depends(get_user_id),
):
    """Purchase a credit pack. Charges the user's saved card immediately.

    Packages:
      - "single": $12 for 1 reservation credit
      - "five_pack": $50 for 5 reservation credits
    """
    if not stripe.api_key:
        raise HTTPException(500, "Stripe not configured")

    if body.package == "five_pack":
        amount_cents = FIVE_PACK_CENTS
        credits_to_add = FIVE_PACK_CREDITS
        description = "5-pack reservation credits"
    elif body.package == "single":
        amount_cents = SINGLE_RESERVATION_CENTS
        credits_to_add = 1
        description = "Single reservation credit"
    else:
        raise HTTPException(400, "Invalid package. Use 'single' or 'five_pack'.")

    # Get user's Stripe customer + default payment method
    profile = await db.get_profile(user_id)
    if not profile:
        raise HTTPException(400, "Profile not found")

    customer_id = profile.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(400, "No payment method on file. Please add a card first.")

    pm = await db.get_default_payment_method(user_id)
    if not pm:
        raise HTTPException(400, "No payment method on file. Please add a card first.")

    # Charge the card via PaymentIntent (off-session, auto-confirm)
    try:
        payment_intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            customer=customer_id,
            payment_method=pm["stripe_payment_method_id"],
            off_session=True,
            confirm=True,
            description=f"TablePass — {description}",
            metadata={
                "tablement_user_id": user_id,
                "package": body.package,
                "credits": str(credits_to_add),
            },
        )
    except stripe.error.CardError as e:
        raise HTTPException(402, f"Card declined: {e.user_message}")
    except Exception as e:
        logger.error("Stripe charge failed for user %s: %s", user_id, e)
        raise HTTPException(500, "Payment failed. Please try again.")

    if payment_intent.status != "succeeded":
        raise HTTPException(402, "Payment requires additional action. Please update your card.")

    # Add credits
    new_balance = await db.add_credits(user_id, credits_to_add)

    # Record transaction
    await db.create_transaction(
        user_id=user_id,
        type="credit_purchase",
        amount_cents=amount_cents,
        credits_delta=credits_to_add,
        stripe_payment_intent_id=payment_intent.id,
        description=description,
    )

    logger.info(
        "User %s purchased %s (%d credits) — balance now %d",
        user_id, body.package, credits_to_add, new_balance,
    )

    return {
        "success": True,
        "credits_added": credits_to_add,
        "new_balance": new_balance,
        "amount_charged": amount_cents / 100,
    }


# ---------------------------------------------------------------------------
# Transactions (billing history)
# ---------------------------------------------------------------------------


@router.get("/transactions", response_model=TransactionListResponse)
async def list_transactions(user_id: str = Depends(get_user_id)):
    """List billing transactions for the user."""
    rows = await db.list_transactions(user_id)
    return TransactionListResponse(
        transactions=[
            TransactionOut(
                id=r["id"],
                type=r["type"],
                amount_cents=r["amount_cents"],
                credits_delta=r.get("credits_delta", 0),
                description=r.get("description"),
                reservation_id=r.get("reservation_id"),
                created_at=r["created_at"],
            )
            for r in rows
        ]
    )


# ---------------------------------------------------------------------------
# Charge-on-success (called from snipe loop, not a route)
# ---------------------------------------------------------------------------


async def charge_for_booking(user_id: str, reservation_id: str) -> dict:
    """Charge the user after a successful booking.

    Priority:
    1. If user has credits > 0 → deduct 1, no Stripe charge
    2. If user has referral_discount → charge $6 (50% off), disable flag
    3. No credits or discount → charge $12 to saved card
    4. No card on file → log warning (reservation already confirmed)

    Returns dict with charge details for logging.
    """
    # Try credits first
    if await db.deduct_credit(user_id):
        credits_remaining = await db.get_user_credits(user_id)
        await db.create_transaction(
            user_id=user_id,
            reservation_id=reservation_id,
            type="credit_used",
            amount_cents=0,
            credits_delta=-1,
            description="Reservation credit used",
        )
        logger.info(
            "User %s: credit used for reservation %s — %d remaining",
            user_id, reservation_id, credits_remaining,
        )
        return {"method": "credit", "remaining": credits_remaining}

    # Check for referral discount (50% off first reservation)
    profile = await db.get_profile(user_id)
    has_referral_discount = (profile or {}).get("referral_discount", False)

    # Determine charge amount
    if has_referral_discount:
        charge_amount = SINGLE_RESERVATION_CENTS // 2  # $6 (50% off)
    else:
        charge_amount = SINGLE_RESERVATION_CENTS  # $12 (full price)

    # Need Stripe to charge card
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        logger.warning("No STRIPE_SECRET_KEY — skipping charge for %s", reservation_id)
        return {"method": "skipped", "reason": "stripe_not_configured"}

    stripe.api_key = stripe_key

    customer_id = (profile or {}).get("stripe_customer_id")
    pm = await db.get_default_payment_method(user_id)

    if not customer_id or not pm:
        logger.warning(
            "User %s has no card on file — reservation %s confirmed but not charged",
            user_id, reservation_id,
        )
        await db.create_transaction(
            user_id=user_id,
            reservation_id=reservation_id,
            type="charge",
            amount_cents=charge_amount,
            credits_delta=0,
            description="Reservation charge UNPAID — no card on file",
        )
        return {"method": "unpaid", "reason": "no_card"}

    try:
        discount_label = " (50% referral discount)" if has_referral_discount else ""
        payment_intent = stripe.PaymentIntent.create(
            amount=charge_amount,
            currency="usd",
            customer=customer_id,
            payment_method=pm["stripe_payment_method_id"],
            off_session=True,
            confirm=True,
            description=f"TablePass — Reservation {reservation_id[:8]}{discount_label}",
            metadata={
                "tablement_user_id": user_id,
                "reservation_id": reservation_id,
                "referral_discount": str(has_referral_discount),
            },
        )

        if payment_intent.status == "succeeded":
            # Disable referral discount after successful use
            if has_referral_discount:
                await db.upsert_profile(user_id, referral_discount=False)

            charge_desc = f"Reservation charge — ${charge_amount / 100:.0f}"
            if has_referral_discount:
                charge_desc += " (50% referral discount)"
            tx_type = "referral_discount" if has_referral_discount else "charge"

            await db.create_transaction(
                user_id=user_id,
                reservation_id=reservation_id,
                type=tx_type,
                amount_cents=charge_amount,
                credits_delta=0,
                stripe_payment_intent_id=payment_intent.id,
                description=charge_desc,
            )
            logger.info(
                "Charged $%d to user %s for reservation %s%s",
                charge_amount // 100, user_id, reservation_id,
                " (referral discount)" if has_referral_discount else "",
            )
            return {"method": "referral_discount" if has_referral_discount else "card", "amount": charge_amount}
        else:
            logger.warning(
                "Payment intent %s status=%s for reservation %s",
                payment_intent.id, payment_intent.status, reservation_id,
            )
            return {"method": "pending", "status": payment_intent.status}

    except stripe.error.CardError as e:
        logger.warning("Card declined for user %s: %s", user_id, e.user_message)
        await db.create_transaction(
            user_id=user_id,
            reservation_id=reservation_id,
            type="charge",
            amount_cents=charge_amount,
            credits_delta=0,
            description=f"Reservation charge FAILED — {e.user_message}",
        )
        return {"method": "failed", "reason": str(e.user_message)}
    except Exception as e:
        logger.error("Stripe charge error for %s: %s", reservation_id, e)
        return {"method": "error", "reason": str(e)}
