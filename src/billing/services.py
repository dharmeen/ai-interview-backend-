import logging
import uuid
from datetime import datetime, timezone

import stripe
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.billing.models import Payment, PaymentStatus
from src.billing.plans import CURRENCY, FREE_MAX_QUESTIONS, PLANS
from src.user.models import User
from src.utils.settings import settings

logger = logging.getLogger(__name__)

BILLING_UNAVAILABLE = "Payments are not available right now. Please try again later."
STRIPE_ERROR = "Could not reach the payment provider. Please try again in a minute."
CHECKOUT_NOT_FOUND = "Checkout session not found"
NO_INTERVIEWS_LEFT = "You have no interviews left. Buy a plan to continue."
FREE_TOO_MANY_QUESTIONS = (
    f"Free interviews have at most {FREE_MAX_QUESTIONS} questions. Buy a plan for longer interviews."
)

# Interview.credit_type values: which balance paid for the interview
CREDIT_FREE = "free"
CREDIT_PAID = "paid"

_client: stripe.StripeClient | None = None


def _get_client() -> stripe.StripeClient:
    # created on first use, so the app can start (and non-billing routes work) without a key
    global _client
    if _client is None:
        if settings.STRIPE_SECRET_KEY is None or not settings.STRIPE_SECRET_KEY.get_secret_value():
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=BILLING_UNAVAILABLE)
        _client = stripe.StripeClient(settings.STRIPE_SECRET_KEY.get_secret_value())
    return _client


# ---------- interview credits ----------


def check_can_create(user: User, num_questions: int) -> None:
    """Fails with 402 if the user can't pay for an interview with this many questions.
    Nothing is spent here: credits are only used when the interview starts."""
    if user.paid_interviews_left > 0:
        return
    if user.free_interviews_left <= 0:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=NO_INTERVIEWS_LEFT)
    if num_questions > FREE_MAX_QUESTIONS:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=FREE_TOO_MANY_QUESTIONS)


def _take_one(db: Session, user_id: uuid.UUID, column) -> bool:
    # a single UPDATE ... WHERE balance > 0, so two parallel starts can't both spend the last credit
    result = db.execute(
        update(User).where(User.id == user_id, column > 0).values({column: column - 1})
    )
    return result.rowcount == 1


def use_interview_credit(db: Session, user_id: uuid.UUID, num_questions: int) -> str:
    """Spends one interview credit and returns CREDIT_FREE or CREDIT_PAID. Caller commits
    (or rolls back, which gives the credit back). Free credits go first when they fit."""
    if num_questions <= FREE_MAX_QUESTIONS and _take_one(db, user_id, User.free_interviews_left):
        return CREDIT_FREE
    if _take_one(db, user_id, User.paid_interviews_left):
        return CREDIT_PAID
    if num_questions > FREE_MAX_QUESTIONS and db.scalar(
        select(User.free_interviews_left).where(User.id == user_id)
    ):
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=FREE_TOO_MANY_QUESTIONS)
    raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=NO_INTERVIEWS_LEFT)


# ---------- checkout ----------


def create_checkout(db: Session, user: User, plan_id: str) -> tuple[str, str]:
    """Creates a Stripe Checkout page for the plan. Returns (checkout_url, session_id)."""
    plan = PLANS[plan_id]
    client = _get_client()

    payment = Payment(
        user_id=user.id,
        plan=plan.id,
        amount_cents=plan.price_cents,
        currency=CURRENCY,
        interviews=plan.interviews,
    )
    db.add(payment)
    db.flush()  # gives payment.id, which goes into the Stripe metadata

    frontend = settings.FRONTEND_URL.rstrip("/")
    try:
        session = client.v1.checkout.sessions.create(
            params={
                "mode": "payment",
                "line_items": [
                    {
                        "quantity": 1,
                        "price_data": {
                            "currency": CURRENCY,
                            "unit_amount": plan.price_cents,
                            "product_data": {
                                "name": f"AI Interview {plan.name}",
                                "description": f"{plan.interviews} interview sessions",
                            },
                        },
                    }
                ],
                "customer_email": user.email,
                # Indian Stripe accounts must collect the billing address for USD (export) payments
                "billing_address_collection": "required",
                "client_reference_id": str(payment.id),
                "metadata": {"payment_id": str(payment.id), "user_id": str(user.id), "plan": plan.id},
                # Stripe replaces {CHECKOUT_SESSION_ID} with the real id
                "success_url": f"{frontend}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
                "cancel_url": f"{frontend}/billing/cancel",
            },
            # retrying with the same key never creates a second checkout for this payment
            options={"idempotency_key": f"checkout-{payment.id}"},
        )
    except stripe.StripeError as e:
        logger.warning("Stripe checkout failed for user %s: %s", user.id, type(e).__name__)
        db.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=STRIPE_ERROR)

    payment.stripe_session_id = session.id
    db.commit()
    return session.url, session.id


def _fulfill(db: Session, session) -> Payment | None:
    """Adds the plan's interviews once the checkout is paid. Safe to call many times for the
    same session (webhook retries, success page): only the first call adds credits.
    Caller commits."""
    # FOR UPDATE: the webhook and the success page can arrive at the same moment
    payment = db.scalar(
        select(Payment).where(Payment.stripe_session_id == session.id).with_for_update()
    )
    if payment is None:
        logger.warning("Stripe session %s has no matching payment", session.id)
        return None
    if payment.status == PaymentStatus.PAID:
        return payment

    if session.payment_status not in ("paid", "no_payment_required"):
        return payment  # e.g. a bank payment that hasn't cleared yet; a later event finishes it
    if session.amount_total != payment.amount_cents or session.currency != payment.currency:
        logger.error("Stripe session %s amount does not match payment %s", session.id, payment.id)
        return payment

    payment.status = PaymentStatus.PAID
    payment.paid_at = datetime.now(timezone.utc)
    payment.stripe_payment_intent_id = session.payment_intent if isinstance(session.payment_intent, str) else None
    db.execute(
        update(User)
        .where(User.id == payment.user_id)
        .values(paid_interviews_left=User.paid_interviews_left + payment.interviews)
    )
    logger.info("Payment %s paid: +%s interviews for user %s", payment.id, payment.interviews, payment.user_id)
    return payment


def _mark(db: Session, session_id: str, new_status: PaymentStatus) -> None:
    db.execute(
        update(Payment)
        .where(Payment.stripe_session_id == session_id, Payment.status == PaymentStatus.PENDING)
        .values(status=new_status)
    )


def handle_webhook(db: Session, payload: bytes, signature: str | None) -> None:
    if settings.STRIPE_WEBHOOK_SECRET is None or not settings.STRIPE_WEBHOOK_SECRET.get_secret_value():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=BILLING_UNAVAILABLE)
    try:
        # checks the Stripe-Signature header, so nobody else can fake a "paid" event
        event = _get_client().construct_event(
            payload, signature, settings.STRIPE_WEBHOOK_SECRET.get_secret_value()
        )
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook")

    session = event.data.object
    if event.type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        _fulfill(db, session)
    elif event.type == "checkout.session.async_payment_failed":
        _mark(db, session.id, PaymentStatus.FAILED)
    elif event.type == "checkout.session.expired":
        _mark(db, session.id, PaymentStatus.EXPIRED)
    db.commit()


def confirm_checkout(db: Session, user: User, session_id: str) -> Payment:
    """Called by the success page. Asks Stripe directly, so credits show up even if the
    webhook is late (or not set up while testing locally)."""
    payment = db.scalar(
        select(Payment).where(Payment.stripe_session_id == session_id, Payment.user_id == user.id)
    )
    if payment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CHECKOUT_NOT_FOUND)
    if payment.status == PaymentStatus.PAID:
        return payment

    try:
        session = _get_client().v1.checkout.sessions.retrieve(session_id)
    except stripe.StripeError as e:
        logger.warning("Stripe retrieve failed for session %s: %s", session_id, type(e).__name__)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=STRIPE_ERROR)

    _fulfill(db, session)
    db.commit()
    db.refresh(payment)
    return payment


def list_payments(db: Session, user: User, limit: int = 20) -> list[Payment]:
    payments = db.scalars(
        select(Payment)
        .where(Payment.user_id == user.id)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .limit(limit)
    ).all()
    return list(payments)
