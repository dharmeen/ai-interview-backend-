from dataclasses import dataclass

# every new account gets this many free interviews, each with at most FREE_MAX_QUESTIONS questions
FREE_INTERVIEWS = 1
FREE_MAX_QUESTIONS = 5

CURRENCY = "usd"


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    price_cents: int  # Stripe works in the smallest currency unit: 2000 = $20.00
    interviews: int  # interview sessions added to the account after payment


# one-time packs, not subscriptions: pay once, get N interview sessions
PLANS: dict[str, Plan] = {
    "starter": Plan(id="starter", name="Starter", price_cents=2000, interviews=3),
    "pro": Plan(id="pro", name="Pro", price_cents=6000, interviews=8),
}
