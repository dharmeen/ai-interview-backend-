from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from sqlalchemy.orm import Session

from src.user import services
from src.user.dependencies import get_current_user
from src.user.models import User
from src.user.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from src.utils.db import get_db
from src.utils.settings import settings

router = APIRouter(prefix="/auth", tags=["Auth"])
users_router = APIRouter(prefix="/users", tags=["Users"])

REFRESH_COOKIE = "refresh_token"
# the browser only sends the cookie to /auth/... routes, not to every request
REFRESH_COOKIE_PATH = "/auth"


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=refresh_token,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="strict",
    )


def _token_response(access_token: str) -> TokenResponse:
    return TokenResponse(
        access_token=access_token, expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )


def _client_info(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    return request.headers.get("user-agent"), ip


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    return services.register_user(db, data)


@router.post("/login", response_model=TokenResponse)
def login(data: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    access_token, refresh_token = services.login_user(db, data, *_client_info(request))
    _set_refresh_cookie(response, refresh_token)
    return _token_response(access_token)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    access_token, new_refresh_token = services.refresh_tokens(db, refresh_token, *_client_info(request))
    _set_refresh_cookie(response, new_refresh_token)
    return _token_response(access_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(refresh_token: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    services.logout_user(db, refresh_token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, httponly=True, secure=settings.COOKIE_SECURE, samesite="strict"
    )
    return response


@users_router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return current_user
