"""User model for login."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, func

from app.database import Base


class User(Base):
    """User account for platform login."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
