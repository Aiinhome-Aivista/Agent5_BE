"""Login router with CAPTCHA support."""
import secrets
import random
import string
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from loguru import logger

from app.database import get_db
from app.models.user import User
from app.schemas.schemas import CaptchaResponse, LoginRequest, LoginResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])

# In-memory captcha store (captcha_id -> {text, expires_at})
_captcha_store = {}


def verify_password(plain: str, stored: str) -> bool:
    """Verify a password by direct comparison."""
    return plain == stored


@router.get("/captcha", response_model=CaptchaResponse)
async def get_captcha():
    """Generate a new CAPTCHA challenge."""
    captcha_id = secrets.token_urlsafe(16)
    challenge_text = "".join(
        random.choices(string.ascii_uppercase + string.digits, k=6)
    )

    _captcha_store[captcha_id] = {
        "text": challenge_text,
        "expires_at": datetime.utcnow() + timedelta(minutes=10),
    }

    return CaptchaResponse(
        captcha_id=captcha_id,
        challenge=challenge_text,
    )


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, db: Session = Depends(get_db)):
    """Login with email and password."""
    # Parse captcha token (format: id:answer)
    parts = req.captcha_token.split(":")
    if len(parts) != 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid captcha token format",
        )

    captcha_id, user_answer = parts

    # Validate captcha
    if captcha_id not in _captcha_store:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CAPTCHA expired or invalid",
        )

    captcha_data = _captcha_store[captcha_id]
    if datetime.utcnow() > captcha_data["expires_at"]:
        del _captcha_store[captcha_id]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CAPTCHA expired",
        )

    if user_answer.upper() != captcha_data["text"].upper():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect CAPTCHA answer",
        )

    # Find user by email
    user = db.query(User).filter(User.email == req.email).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Verify password
    if not verify_password(req.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Clean up captcha
    del _captcha_store[captcha_id]

    logger.info("User logged in: {}", user.email)

    return LoginResponse(
        user_id=user.id,
        email=user.email,
    )
