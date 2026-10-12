from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.user.models import RefreshToken, User
from src.user.schemas import LoginRequest, RegisterRequest, normalize_phone
from src.utils.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from src.utils.settings import settings

EMAIL_TAKEN = "Email already registered"
PHONE_TAKEN = "Phone number already registered"
INVALID_CREDENTIALS = "Invalid email/phone or password"
INVALID_REFRESH = "Invalid or expired refresh token"

MAX_FAILED_ATTEMPTS = 5
LOCK_DURATION = timedelta(minutes=15)

# used when the user doesn't exist, so a wrong email takes as long as a wrong password
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def register_user(db: Session, data: RegisterRequest) -> User:
    if db.scalar(select(User.id).where(User.email == data.email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=EMAIL_TAKEN)
    if db.scalar(select(User.id).where(User.phone == data.phone)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=PHONE_TAKEN)

    user = User(
        name=data.name,
        email=data.email,
        phone=data.phone,
        password_hash=hash_password(data.password),
        state=data.state,
        city=data.city,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as e:
        # two requests with the same email/phone at the same moment
        db.rollback()
        detail = PHONE_TAKEN if "phone" in str(e.orig) else EMAIL_TAKEN
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    db.refresh(user)
    return user


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _find_user(db: Session, identifier: str) -> User | None:
    identifier = identifier.strip()
    if "@" in identifier:
        return db.scalar(select(User).where(User.email == identifier.lower()))
    try:
        phone = normalize_phone(identifier)
    except ValueError:
        return None
    return db.scalar(select(User).where(User.phone == phone))


def _issue_tokens(
    db: Session, user: User, user_agent: str | None, ip_address: str | None
) -> tuple[str, str]:
    """Creates an access token and a new refresh token row. Caller commits."""
    refresh_token = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh_token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            user_agent=user_agent[:255] if user_agent else None,
            ip_address=ip_address,
        )
    )
    return create_access_token(user.id), refresh_token


def login_user(
    db: Session, data: LoginRequest, user_agent: str | None, ip_address: str | None
) -> tuple[str, str]:
    """Returns (access_token, refresh_token)."""
    user = _find_user(db, data.identifier)
    if user is None:
        verify_password(data.password, _DUMMY_HASH)
        raise _unauthorized(INVALID_CREDENTIALS)

    now = datetime.now(timezone.utc)
    if user.locked_until and user.locked_until > now:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account locked after too many failed attempts. Try again in 15 minutes.",
        )

    if not verify_password(data.password, user.password_hash):
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = now + LOCK_DURATION
            user.failed_login_attempts = 0
        db.commit()
        raise _unauthorized(INVALID_CREDENTIALS)

    # checked only after the password is right, so it doesn't reveal which accounts exist
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    user.failed_login_attempts = 0
    user.locked_until = None
    tokens = _issue_tokens(db, user, user_agent, ip_address)
    db.commit()
    return tokens


def refresh_tokens(
    db: Session, refresh_token: str | None, user_agent: str | None, ip_address: str | None
) -> tuple[str, str]:
    """Rotation: the old refresh token is revoked and a new pair is returned."""
    if not refresh_token:
        raise _unauthorized(INVALID_REFRESH)

    # FOR UPDATE locks the row, so two requests can't both use the same token
    token = db.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(refresh_token))
        .with_for_update()
    )
    if token is None:
        raise _unauthorized(INVALID_REFRESH)

    now = datetime.now(timezone.utc)
    if token.revoked_at is not None:
        # an already-used token came back: it was probably stolen, so log out every session
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == token.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        db.commit()
        raise _unauthorized(INVALID_REFRESH)

    user = db.get(User, token.user_id)
    if token.expires_at <= now or user is None or not user.is_active:
        raise _unauthorized(INVALID_REFRESH)

    token.revoked_at = now
    tokens = _issue_tokens(db, user, user_agent, ip_address)
    db.commit()
    return tokens


def logout_user(db: Session, refresh_token: str | None) -> None:
    if not refresh_token:
        return
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(refresh_token), RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    db.commit()
