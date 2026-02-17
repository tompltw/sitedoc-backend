"""
Stripe billing routes.

Current state: stubs that return clear errors until Stripe is configured.
Wire up by setting STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET in .env.

Plans:
  - starter:  $29/mo — 1 site, 5 issues/mo
  - pro:      $79/mo — 5 sites, unlimited issues
  - agency:  $199/mo — unlimited sites
"""
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_customer
from src.api.schemas import BillingPortalResponse, CheckoutSessionResponse
from src.db.models import Customer
from src.db.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)

PLANS = {
    "starter": {"name": "Starter", "price_id": os.getenv("STRIPE_PRICE_STARTER", ""), "amount": 2900},
    "pro":     {"name": "Pro",     "price_id": os.getenv("STRIPE_PRICE_PRO", ""),     "amount": 7900},
    "agency":  {"name": "Agency",  "price_id": os.getenv("STRIPE_PRICE_AGENCY", ""), "amount": 19900},
}


def _stripe():
    """Lazy Stripe import — raises clearly if not configured."""
    secret = os.getenv("STRIPE_SECRET_KEY", "")
    if not secret or secret.startswith("sk_test_placeholder"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing not configured. Set STRIPE_SECRET_KEY in .env.",
        )
    import stripe
    stripe.api_key = secret
    return stripe


@router.get("/plans")
async def list_plans():
    """Return available plans. Always works — no Stripe needed."""
    return [
        {
            "id": plan_id,
            "name": plan["name"],
            "amount_cents": plan["amount"],
            "currency": "usd",
            "interval": "month",
        }
        for plan_id, plan in PLANS.items()
    ]


@router.post("/checkout/{plan_id}", response_model=CheckoutSessionResponse)
async def create_checkout_session(
    plan_id: str,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Create a Stripe Checkout session to subscribe to a plan."""
    if plan_id not in PLANS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown plan")

    plan = PLANS[plan_id]
    if not plan["price_id"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"STRIPE_PRICE_{plan_id.upper()} not set in .env",
        )

    stripe = _stripe()
    app_url = os.getenv("APP_URL", "http://localhost:3000")

    # Create or retrieve Stripe customer
    stripe_customer_id = current_customer.stripe_customer_id
    if not stripe_customer_id:
        sc = stripe.Customer.create(
            email=current_customer.email,
            name=current_customer.name,
            metadata={"sitedoc_customer_id": str(current_customer.id)},
        )
        stripe_customer_id = sc.id
        # Save back to DB
        from sqlalchemy import text
        await db.execute(
            text("UPDATE customers SET stripe_customer_id = :sid WHERE id = :id"),
            {"sid": stripe_customer_id, "id": str(current_customer.id)},
        )
        await db.commit()

    session = stripe.checkout.Session.create(
        customer=stripe_customer_id,
        payment_method_types=["card"],
        line_items=[{"price": plan["price_id"], "quantity": 1}],
        mode="subscription",
        success_url=f"{app_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{app_url}/billing",
        metadata={"plan_id": plan_id, "customer_id": str(current_customer.id)},
    )

    return {"checkout_url": session.url, "session_id": session.id}


@router.post("/portal", response_model=BillingPortalResponse)
async def billing_portal(
    current_customer: Customer = Depends(get_current_customer),
):
    """Return a Stripe Customer Portal URL for managing subscriptions."""
    if not current_customer.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No billing account found. Subscribe to a plan first.",
        )

    stripe = _stripe()
    app_url = os.getenv("APP_URL", "http://localhost:3000")

    session = stripe.billing_portal.Session.create(
        customer=current_customer.stripe_customer_id,
        return_url=f"{app_url}/billing",
    )

    return {"portal_url": session.url}


@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Handle Stripe webhook events.
    Set STRIPE_WEBHOOK_SECRET and point Stripe → POST /api/v1/billing/webhook.
    """
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret or webhook_secret.startswith("whsec_placeholder"):
        raise HTTPException(status_code=400, detail="Webhook secret not configured")

    stripe = _stripe()
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    from sqlalchemy import text

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_id = session.get("metadata", {}).get("customer_id")
        plan_id = session.get("metadata", {}).get("plan_id")
        stripe_subscription_id = session.get("subscription")

        if customer_id:
            await db.execute(
                text("""
                    UPDATE customers
                    SET plan = :plan, stripe_subscription_id = :sub_id
                    WHERE id = :id
                """),
                {"plan": plan_id, "sub_id": stripe_subscription_id, "id": customer_id},
            )
            await db.commit()
            logger.info("Customer %s subscribed to %s", customer_id, plan_id)

    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
        sub = event["data"]["object"]
        stripe_customer_id = sub.get("customer")
        if stripe_customer_id:
            await db.execute(
                text("UPDATE customers SET plan = 'free' WHERE stripe_customer_id = :sid"),
                {"sid": stripe_customer_id},
            )
            await db.commit()
            logger.info("Subscription ended for Stripe customer %s", stripe_customer_id)

    return {"received": True}
