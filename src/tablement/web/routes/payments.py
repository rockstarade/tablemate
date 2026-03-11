"""Stripe payment routes — setup intents, subscriptions, payment methods, credits.

Flow:
1. POST /setup-intent    → creates Stripe SetupIntent + customer
2. POST /methods         → saves a payment method after Stripe.js confirmation
3. GET  /methods         → lists saved payment methods
4. DELETE /methods/:id   → removes a payment method
5. GET  /credits         → check credit balance
6. POST /buy-credits     → purchase a credit pack ($50 five-pack)
7. GET  /transactions    → billing history
8. POST /subscribe       → create a Pro or VIP subscription
9. POST /cancel-subscription → cancel at period end
10. POST /change-plan    → switch between Pro and VIP
11. GET  /plan           → current plan info
12. POST /webhook        → Stripe webhook handler

Charge-on-success:
  charge_for_booking() is called from the snipe loop when a reservation
  is confirmed. Plan-aware: beta=free, pro/vip=included or overage,
  single/free=$15 or credit.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from tablement.web import db
from tablement.web.deps import get_user_id
from tablement.web.ratelimit import limiter
from tablement.web.schemas import (
    BuyCreditsRequest,
    CreditBalanceResponse,
    PaymentMethodListResponse,
    PaymentMethodOut,
    PlanInfoResponse,
    SavePaymentMethodRequest,
    SetupIntentResponse,
    SubscribeRequest,
    SubscriptionResponse,
    TransactionListResponse,
    TransactionOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Initialize Stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

# ─── Pricing (in cents) ──────────────────────────────────────────────────────

# Per-reservation pricing (pay-as-you-go)
SINGLE_BOOKING_CENTS = 1200           # $12 per booking

# Subscription tiers
PRO_MONTHLY_CENTS = 2900              # $29/mo
PRO_INCLUDED_BOOKINGS = 5
PRO_OVERAGE_CENTS = 1000              # $10 per extra booking

VIP_MONTHLY_CENTS = 6900              # $69/mo
VIP_INCLUDED_BOOKINGS = 10
VIP_OVERAGE_CENTS = 800               # $8 per extra booking

# Legacy credit packs (kept for backwards compat)
FIVE_PACK_CENTS = 4800                # $48 (5 × $12 = $60 value, save $12)
FIVE_PACK_CREDITS = 5

# Old single price constant alias (used elsewhere)
SINGLE_RESERVATION_CENTS = SINGLE_BOOKING_CENTS

# Plan → Stripe Price ID mapping (set in .env)
STRIPE_PRICES = {
    "pro": os.environ.get("STRIPE_PRICE_PRO", ""),
    "vip": os.environ.get("STRIPE_PRICE_VIP", ""),
}

PLAN_LIMITS = {
    "pro": {"included": PRO_INCLUDED_BOOKINGS, "overage_cents": PRO_OVERAGE_CENTS},
    "vip": {"included": VIP_INCLUDED_BOOKINGS, "overage_cents": VIP_OVERAGE_CENTS},
}


def _ensure_stripe():
    """Reload Stripe key from env (in case loaded after module import)."""
    if not stripe.api_key:
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(500, "Stripe not configured")


async def _ensure_customer(user_id: str) -> str:
    """Get or create Stripe customer for a user. Returns customer_id."""
    profile = await db.get_profile(user_id)
    if not profile:
        raise HTTPException(400, "Profile not found")

    customer_id = profile.get("stripe_customer_id")
    if not customer_id:
        customer = stripe.Customer.create(
            metadata={"tablement_user_id": user_id},
        )
        customer_id = customer.id
        await db.upsert_profile(user_id, stripe_customer_id=customer_id)
        logger.info("Created Stripe customer %s for user %s", customer_id, user_id)
    return customer_id


# ---------------------------------------------------------------------------
# Setup Intent + Payment Methods
# ---------------------------------------------------------------------------


@router.post("/setup-intent", response_model=SetupIntentResponse)
@limiter.limit("10/minute")
async def create_setup_intent(request: Request, user_id: str = Depends(get_user_id)):
    """Create a Stripe SetupIntent for the frontend to collect card details."""
    _ensure_stripe()
    customer_id = await _ensure_customer(user_id)

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
    """Save a payment method after Stripe.js has confirmed the SetupIntent."""
    _ensure_stripe()

    try:
        pm = stripe.PaymentMethod.retrieve(body.stripe_payment_method_id)
    except Exception as e:
        logger.error("Stripe PaymentMethod retrieve failed: %s", e)
        raise HTTPException(400, "Invalid payment method. Please try again.")

    card = pm.get("card", {})
    brand = card.get("brand", "unknown")
    last_four = card.get("last4", "????")

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
    """Purchase a credit pack. Charges the user's saved card immediately."""
    _ensure_stripe()

    if body.package == "five_pack":
        amount_cents = FIVE_PACK_CENTS
        credits_to_add = FIVE_PACK_CREDITS
        description = "5-pack reservation credits"
    elif body.package == "single":
        amount_cents = SINGLE_BOOKING_CENTS
        credits_to_add = 1
        description = "Single reservation credit"
    else:
        raise HTTPException(400, "Invalid package. Use 'single' or 'five_pack'.")

    customer_id = await _ensure_customer(user_id)
    pm = await db.get_default_payment_method(user_id)
    if not pm:
        raise HTTPException(400, "No payment method on file. Please add a card first.")

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

    new_balance = await db.add_credits(user_id, credits_to_add)

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
# Subscriptions
# ---------------------------------------------------------------------------


@router.get("/plan", response_model=PlanInfoResponse)
async def get_plan(user_id: str = Depends(get_user_id)):
    """Get the user's current plan info."""
    plan_info = await db.get_user_plan(user_id)
    credits = await db.get_user_credits(user_id)
    plan = plan_info.get("plan", "free")
    limits = PLAN_LIMITS.get(plan, {})

    return PlanInfoResponse(
        plan=plan,
        bookings_used=plan_info.get("plan_bookings_used", 0),
        bookings_included=limits.get("included", 0),
        credits=credits,
        stripe_subscription_id=plan_info.get("stripe_subscription_id"),
        period_end=plan_info.get("plan_period_end"),
    )


@router.post("/subscribe")
@limiter.limit("5/minute")
async def subscribe(
    request: Request,
    body: SubscribeRequest,
    user_id: str = Depends(get_user_id),
):
    """Create a Pro or VIP subscription.

    Requires a saved payment method. Creates a Stripe Subscription
    with the matching recurring Price.
    """
    _ensure_stripe()

    if body.plan not in ("pro", "vip"):
        raise HTTPException(400, "Invalid plan. Use 'pro' or 'vip'.")

    # Reload price IDs from env (might not be set at import time)
    price_id = os.environ.get(
        f"STRIPE_PRICE_{'PRO' if body.plan == 'pro' else 'VIP'}", ""
    )
    if not price_id:
        raise HTTPException(500, f"Stripe Price for {body.plan} not configured")

    # Check not already subscribed
    profile = await db.get_profile(user_id)
    existing_sub = (profile or {}).get("stripe_subscription_id")
    if existing_sub:
        # Already has a subscription — use change-plan instead
        raise HTTPException(
            400,
            "Already subscribed. Use /change-plan to switch plans.",
        )

    customer_id = await _ensure_customer(user_id)
    pm = await db.get_default_payment_method(user_id)
    if not pm:
        raise HTTPException(400, "No payment method on file. Please add a card first.")

    # Attach the payment method to the Stripe customer & set as default
    try:
        stripe.PaymentMethod.attach(
            pm["stripe_payment_method_id"], customer=customer_id,
        )
    except stripe.error.InvalidRequestError:
        pass  # Already attached

    stripe.Customer.modify(
        customer_id,
        invoice_settings={"default_payment_method": pm["stripe_payment_method_id"]},
    )

    # Create the subscription
    try:
        subscription = stripe.Subscription.create(
            customer=customer_id,
            items=[{"price": price_id}],
            metadata={
                "tablement_user_id": user_id,
                "plan": body.plan,
            },
            expand=["latest_invoice.payment_intent"],
        )
    except stripe.error.CardError as e:
        raise HTTPException(402, f"Card declined: {e.user_message}")
    except Exception as e:
        logger.error("Subscription creation failed for user %s: %s", user_id, e)
        raise HTTPException(500, "Subscription failed. Please try again.")

    # Check status
    if subscription.status not in ("active", "trialing"):
        # Payment might require action
        raise HTTPException(
            402,
            "Subscription requires additional payment action. Please update your card.",
        )

    # Update profile with subscription info
    period_start = datetime.fromtimestamp(
        subscription.current_period_start, tz=timezone.utc
    ).isoformat()
    period_end = datetime.fromtimestamp(
        subscription.current_period_end, tz=timezone.utc
    ).isoformat()

    await db.set_user_plan(
        user_id,
        plan=body.plan,
        stripe_subscription_id=subscription.id,
        plan_period_start=period_start,
        plan_period_end=period_end,
    )
    await db.reset_plan_bookings(user_id)

    # Record transaction
    amount = PRO_MONTHLY_CENTS if body.plan == "pro" else VIP_MONTHLY_CENTS
    await db.create_transaction(
        user_id=user_id,
        type="subscription",
        amount_cents=amount,
        credits_delta=0,
        stripe_payment_intent_id=subscription.id,
        description=f"TablePass {body.plan.upper()} subscription started",
    )

    logger.info("User %s subscribed to %s plan (sub %s)", user_id, body.plan, subscription.id)

    limits = PLAN_LIMITS[body.plan]
    return {
        "success": True,
        "plan": body.plan,
        "subscription_id": subscription.id,
        "bookings_included": limits["included"],
        "period_end": period_end,
    }


@router.post("/cancel-subscription")
async def cancel_subscription(user_id: str = Depends(get_user_id)):
    """Cancel subscription at period end."""
    _ensure_stripe()

    profile = await db.get_profile(user_id)
    sub_id = (profile or {}).get("stripe_subscription_id")
    if not sub_id:
        raise HTTPException(400, "No active subscription")

    try:
        # Cancel at period end (user keeps access until then)
        stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
    except Exception as e:
        logger.error("Cancel subscription failed for %s: %s", user_id, e)
        raise HTTPException(500, "Failed to cancel. Please try again.")

    logger.info("User %s cancelled subscription %s (at period end)", user_id, sub_id)

    return {
        "success": True,
        "message": "Subscription will cancel at the end of the billing period.",
        "period_end": (profile or {}).get("plan_period_end"),
    }


@router.post("/change-plan")
@limiter.limit("5/minute")
async def change_plan(
    request: Request,
    body: SubscribeRequest,
    user_id: str = Depends(get_user_id),
):
    """Switch between Pro and VIP plans."""
    _ensure_stripe()

    if body.plan not in ("pro", "vip"):
        raise HTTPException(400, "Invalid plan. Use 'pro' or 'vip'.")

    profile = await db.get_profile(user_id)
    current_plan = (profile or {}).get("plan", "free")
    sub_id = (profile or {}).get("stripe_subscription_id")

    if current_plan == body.plan:
        raise HTTPException(400, f"Already on {body.plan} plan")

    if not sub_id:
        raise HTTPException(400, "No active subscription. Use /subscribe to start one.")

    price_id = os.environ.get(
        f"STRIPE_PRICE_{'PRO' if body.plan == 'pro' else 'VIP'}", ""
    )
    if not price_id:
        raise HTTPException(500, f"Stripe Price for {body.plan} not configured")

    try:
        subscription = stripe.Subscription.retrieve(sub_id)
        stripe.Subscription.modify(
            sub_id,
            items=[{
                "id": subscription["items"]["data"][0].id,
                "price": price_id,
            }],
            proration_behavior="create_prorations",
            metadata={"plan": body.plan},
        )
    except Exception as e:
        logger.error("Plan change failed for %s: %s", user_id, e)
        raise HTTPException(500, "Plan change failed. Please try again.")

    await db.upsert_profile(user_id, plan=body.plan)
    logger.info("User %s changed plan from %s to %s", user_id, current_plan, body.plan)

    limits = PLAN_LIMITS[body.plan]
    return {
        "success": True,
        "plan": body.plan,
        "bookings_included": limits["included"],
    }


# ---------------------------------------------------------------------------
# Stripe Webhook
# ---------------------------------------------------------------------------


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events.

    Events:
    - invoice.payment_succeeded → reset bookings counter, update period
    - customer.subscription.deleted → revert to free plan
    - invoice.payment_failed → log warning
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    if not webhook_secret:
        logger.warning("STRIPE_WEBHOOK_SECRET not set — accepting webhook without verification")
        try:
            event = stripe.Event.construct_from(
                stripe.util.convert_to_stripe_object(
                    __import__("json").loads(payload)
                ),
                stripe.api_key,
            )
        except Exception as e:
            logger.error("Webhook parse failed: %s", e)
            return JSONResponse({"error": "Invalid payload"}, status_code=400)
    else:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except stripe.error.SignatureVerificationError:
            logger.warning("Webhook signature verification failed")
            return JSONResponse({"error": "Invalid signature"}, status_code=400)
        except Exception as e:
            logger.error("Webhook construct failed: %s", e)
            return JSONResponse({"error": "Invalid payload"}, status_code=400)

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    if event_type == "invoice.payment_succeeded":
        await _handle_invoice_paid(data)
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(data)
    elif event_type == "invoice.payment_failed":
        await _handle_invoice_failed(data)
    else:
        logger.debug("Unhandled webhook event: %s", event_type)

    return JSONResponse({"received": True})


async def _handle_invoice_paid(invoice: dict) -> None:
    """Reset bookings counter and update period on successful renewal."""
    sub_id = invoice.get("subscription")
    customer_id = invoice.get("customer")
    if not sub_id:
        return

    # Find user by subscription ID
    # We need to look up by stripe_subscription_id
    try:
        client = db.get_service_client()
        resp = await client.table("profiles").select("id, plan").eq(
            "stripe_subscription_id", sub_id
        ).execute()
        if not resp.data:
            logger.warning("Webhook: no user found for subscription %s", sub_id)
            return
        user = resp.data[0]
        user_id = user["id"]
    except Exception as e:
        logger.error("Webhook user lookup failed: %s", e)
        return

    # Get updated period from Stripe
    try:
        subscription = stripe.Subscription.retrieve(sub_id)
        period_start = datetime.fromtimestamp(
            subscription.current_period_start, tz=timezone.utc
        ).isoformat()
        period_end = datetime.fromtimestamp(
            subscription.current_period_end, tz=timezone.utc
        ).isoformat()
    except Exception as e:
        logger.error("Webhook: failed to fetch subscription %s: %s", sub_id, e)
        period_start = None
        period_end = None

    # Reset bookings counter for new period
    await db.reset_plan_bookings(user_id)
    if period_start and period_end:
        await db.upsert_profile(
            user_id,
            plan_period_start=period_start,
            plan_period_end=period_end,
        )

    logger.info(
        "Webhook: invoice paid for user %s (sub %s) — bookings reset",
        user_id, sub_id,
    )


async def _handle_subscription_deleted(subscription: dict) -> None:
    """Revert user to free plan when subscription is deleted/expired."""
    sub_id = subscription.get("id")
    if not sub_id:
        return

    try:
        client = db.get_service_client()
        resp = await client.table("profiles").select("id").eq(
            "stripe_subscription_id", sub_id
        ).execute()
        if not resp.data:
            logger.warning("Webhook: no user found for deleted subscription %s", sub_id)
            return
        user_id = resp.data[0]["id"]
    except Exception as e:
        logger.error("Webhook user lookup failed: %s", e)
        return

    await db.set_user_plan(
        user_id,
        plan="free",
        stripe_subscription_id=None,
        plan_period_start=None,
        plan_period_end=None,
    )
    await db.reset_plan_bookings(user_id)

    logger.info("Webhook: subscription %s deleted — user %s reverted to free", sub_id, user_id)


async def _handle_invoice_failed(invoice: dict) -> None:
    """Log a warning when a subscription payment fails."""
    sub_id = invoice.get("subscription")
    customer_id = invoice.get("customer")
    logger.warning(
        "Webhook: invoice payment failed for subscription %s (customer %s)",
        sub_id, customer_id,
    )


# ---------------------------------------------------------------------------
# Charge-on-success (called from snipe loop, not a route)
# ---------------------------------------------------------------------------


async def charge_for_booking(user_id: str, reservation_id: str) -> dict:
    """Charge the user after a successful booking.

    Plan-aware priority:
    1. beta plan → free, no charge
    2. pro plan → included bookings free, then $8 overage
    3. vip plan → included bookings free, then $6 overage
    4. credits > 0 → deduct 1 credit
    5. referral_discount → charge $7.50 (50% of $15)
    6. Default → charge $15 to saved card
    7. No card → log as UNPAID (don't fail reservation)
    """
    profile = await db.get_profile(user_id)
    if not profile:
        logger.warning("charge_for_booking: no profile for user %s", user_id)
        return {"method": "skipped", "reason": "no_profile"}

    plan = (profile or {}).get("plan", "free")

    # ── Beta: free ──
    if plan == "beta":
        await db.create_transaction(
            user_id=user_id,
            reservation_id=reservation_id,
            type="charge",
            amount_cents=0,
            credits_delta=0,
            description="Beta access — free booking",
        )
        logger.info("User %s: beta free booking for %s", user_id, reservation_id)
        return {"method": "beta", "amount": 0}

    # ── Pro / VIP: included bookings or overage ──
    if plan in ("pro", "vip"):
        limits = PLAN_LIMITS[plan]
        bookings_used = (profile or {}).get("plan_bookings_used", 0)

        if bookings_used < limits["included"]:
            # Included in subscription — no charge
            new_count = await db.increment_plan_bookings(user_id)
            await db.create_transaction(
                user_id=user_id,
                reservation_id=reservation_id,
                type="charge",
                amount_cents=0,
                credits_delta=0,
                description=f"{plan.upper()} included booking ({new_count}/{limits['included']})",
            )
            logger.info(
                "User %s: %s included booking %d/%d for %s",
                user_id, plan, new_count, limits["included"], reservation_id,
            )
            return {
                "method": "subscription_included",
                "plan": plan,
                "bookings_used": new_count,
                "bookings_included": limits["included"],
            }
        else:
            # Overage charge
            overage_cents = limits["overage_cents"]
            charge_result = await _charge_card(
                user_id, reservation_id, profile, overage_cents,
                f"{plan.upper()} overage — ${overage_cents / 100:.0f}",
            )
            if charge_result.get("method") == "card":
                new_count = await db.increment_plan_bookings(user_id)
                charge_result["bookings_used"] = new_count
            return charge_result

    # ── Free / Single: credits → referral → standard charge ──

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

    # Check for referral discount (50% off)
    has_referral_discount = (profile or {}).get("referral_discount", False)
    if has_referral_discount:
        charge_amount = SINGLE_BOOKING_CENTS // 2  # $7.50
    else:
        charge_amount = SINGLE_BOOKING_CENTS  # $15

    return await _charge_card(
        user_id, reservation_id, profile, charge_amount,
        "Reservation charge" + (" (50% referral discount)" if has_referral_discount else ""),
        has_referral_discount=has_referral_discount,
    )


async def _charge_card(
    user_id: str,
    reservation_id: str,
    profile: dict,
    amount_cents: int,
    description: str,
    has_referral_discount: bool = False,
) -> dict:
    """Charge the user's saved card. Shared by subscription overage and pay-per-use."""
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
            amount_cents=amount_cents,
            credits_delta=0,
            description=f"{description} UNPAID — no card on file",
        )
        return {"method": "unpaid", "reason": "no_card"}

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
                "reservation_id": reservation_id,
            },
            idempotency_key=f"charge-{reservation_id}-{description[:30]}",
        )

        if payment_intent.status == "succeeded":
            if has_referral_discount:
                await db.upsert_profile(user_id, referral_discount=False)

            tx_type = "referral_discount" if has_referral_discount else "charge"
            await db.create_transaction(
                user_id=user_id,
                reservation_id=reservation_id,
                type=tx_type,
                amount_cents=amount_cents,
                credits_delta=0,
                stripe_payment_intent_id=payment_intent.id,
                description=f"{description} — ${amount_cents / 100:.2f}",
            )
            logger.info(
                "Charged $%.2f to user %s for reservation %s",
                amount_cents / 100, user_id, reservation_id,
            )
            return {"method": "card", "amount": amount_cents}
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
            amount_cents=amount_cents,
            credits_delta=0,
            description=f"{description} FAILED — {e.user_message}",
        )
        return {"method": "failed", "reason": str(e.user_message)}
    except Exception as e:
        logger.error("Stripe charge error for %s: %s", reservation_id, e)
        return {"method": "error", "reason": str(e)}
