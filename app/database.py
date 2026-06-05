"""
Database setup — SQLAlchemy with MySQL.
Provides Base, engine, SessionLocal, and FastAPI dependency `get_db`.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
    echo=settings.DEBUG,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables. Call once at startup."""
    # Import models so they register with Base.metadata
    from app.models import (  # noqa: F401
        recommendation,
        action_history,
        telemetry,
        chat_history,
        cloud_account,
        users,
    )
    Base.metadata.create_all(bind=engine)
