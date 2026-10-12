import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.utils.constants import INDIAN_STATES

# lowercase name -> official name, for case-insensitive state matching
_STATE_LOOKUP = {s.lower(): s for s in INDIAN_STATES}

NAME_RE = re.compile(r"^[A-Za-z. ]+$")
CITY_RE = re.compile(r"^[A-Za-z ]+$")
PHONE_RE = re.compile(r"^[6-9]\d{9}$")


def normalize_phone(v: str) -> str:
    """'+91 98765 43210' / '09876543210' / '9876543210' -> '+919876543210'"""
    digits = re.sub(r"[\s-]", "", v)
    if digits.startswith("+91"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if not PHONE_RE.match(digits):
        raise ValueError("Enter a valid 10-digit Indian mobile number")
    return f"+91{digits}"


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    phone: str
    password: str = Field(min_length=8, max_length=128)
    state: str
    city: str = Field(min_length=2, max_length=100)

    # mode="before" runs before the length checks, so " Ab " is stripped first
    @field_validator("name", "city", mode="before")
    @classmethod
    def strip_spaces(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("Name can contain only letters, spaces and dots")
        return v

    @field_validator("city")
    @classmethod
    def validate_city(cls, v: str) -> str:
        if not CITY_RE.match(v):
            raise ValueError("City can contain only letters and spaces")
        return v

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, v: str) -> str:
        return v.lower()

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("Password must contain at least one letter and one number")
        return v

    @field_validator("state")
    @classmethod
    def validate_state(cls, v: str) -> str:
        official = _STATE_LOOKUP.get(v.strip().lower())
        if official is None:
            raise ValueError("Select a valid Indian state or union territory")
        return official


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    phone: str
    state: str
    city: str
    email_verified: bool
    phone_verified: bool
    created_at: datetime


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=255, description="Email or phone number")
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
