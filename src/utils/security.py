import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash

from src.utils.settings import settings

# Argon2 with pwdlib's recommended settings
password_hash = PasswordHash.recommended()

JWT_ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return password_hash.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return password_hash.verify(plain, hashed)


def create_access_token(user_id: uuid.UUID) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "exp": expire, "type": "access"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Returns the payload. Raises jwt.InvalidTokenError if the token is bad or expired."""
    payload = jwt.decode(
        token, settings.JWT_SECRET, algorithms=[JWT_ALGORITHM], options={"require": ["sub", "exp"]}
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return payload


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(64)


def hash_token(token: str) -> str:
    # refresh tokens are long random strings, so a fast SHA-256 is enough (no Argon2 needed)
    return hashlib.sha256(token.encode()).hexdigest()
