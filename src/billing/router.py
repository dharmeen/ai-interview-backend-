from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from src.billing import services
from src.billing.plans import CURRENCY, FREE_INTERVIEWS, FREE_MAX_QUESTIONS, PLANS
from src.billing.schemas import (
    BillingStatusResponse,
    CheckoutRequest,
    CheckoutResponse,
    PaymentResponse,
    PlanResponse,
    PlansResponse,
)
from src.user.dependencies import get_current_user
from src.user.models import User
from src.utils.db import get_db

router = APIRouter(prefix="/billing", tags=["Billing"])


@router.get("/plans", response_model=PlansResponse)
def list_plans():
    # public: the pricing page works before login
    return PlansResponse(
        free_interviews=FREE_INTERVIEWS,
        free_max_questions=FREE_MAX_QUESTIONS,
        plans=[
            PlanResponse(
                id=p.id, name=p.name, price_cents=p.price_cents, currency=CURRENCY, interviews=p.interviews
            )
            for p in PLANS.values()
        ],
    )


@router.get("/me", response_model=BillingStatusResponse)
def billing_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return BillingStatusResponse(
        free_interviews_left=current_user.free_interviews_left,
        free_max_questions=FREE_MAX_QUESTIONS,
        paid_interviews_left=current_user.paid_interviews_left,
        payments=services.list_payments(db, current_user),
    )


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout(
    data: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    checkout_url, session_id = services.create_checkout(db, current_user, data.plan)
    return CheckoutResponse(checkout_url=checkout_url, session_id=session_id)


@router.post("/checkout/{session_id}/confirm", response_model=PaymentResponse)
def confirm_checkout(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return services.confirm_checkout(db, current_user, session_id)


@router.post("/webhook", status_code=status.HTTP_200_OK, include_in_schema=False)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    # async only to read the raw body: the signature check needs the exact bytes Stripe sent
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    await run_in_threadpool(services.handle_webhook, db, payload, signature)
    return {"received": True}
