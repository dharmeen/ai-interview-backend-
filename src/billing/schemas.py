import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.billing.models import PaymentStatus


class PlanResponse(BaseModel):
    id: str
    name: str
    price_cents: int
    currency: str
    interviews: int


class PlansResponse(BaseModel):
    free_interviews: int
    free_max_questions: int
    plans: list[PlanResponse]


class CheckoutRequest(BaseModel):
    plan: Literal["starter", "pro"]


class CheckoutResponse(BaseModel):
    checkout_url: str  # send the browser here
    session_id: str


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    plan: str
    amount_cents: int
    currency: str
    interviews: int
    status: PaymentStatus
    paid_at: datetime | None
    created_at: datetime


class BillingStatusResponse(BaseModel):
    free_interviews_left: int
    free_max_questions: int
    paid_interviews_left: int
    payments: list[PaymentResponse]  # newest first
