from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.billing.router import router as billing_router
from src.interview.router import profiles_router, router as interview_router
from src.user.router import router as auth_router, users_router
from src.utils.settings import settings

# Tables are created by Alembic migrations (alembic upgrade head), not create_all

app = FastAPI(title="Task Management Application")

# lets the Next.js frontend call the API and send the refresh cookie
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(interview_router)
app.include_router(profiles_router)
app.include_router(billing_router)
