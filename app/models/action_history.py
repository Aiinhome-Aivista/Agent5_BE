"""Action history — audit trail of every executed optimization."""
from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, Text, ForeignKey
from sqlalchemy.sql import func
from app.database import Base


class ActionHistory(Base):
    __tablename__ = "action_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    recommendation_id = Column(Integer, ForeignKey("recommendations.id"), nullable=True)
    provider = Column(String(20), nullable=False, index=True)
    account_id = Column(String(255), nullable=False, index=True)
    resource_id = Column(String(500), nullable=True)
    action_type = Column(String(100), nullable=False)

    status = Column(String(30), nullable=False, index=True)
    # initiated, succeeded, failed, rolled_back, dry_run

    actor = Column(String(100), default="agent", nullable=False)  # agent | user_email
    request_payload = Column(JSON, nullable=True)
    response_payload = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)

    metrics_before = Column(JSON, nullable=True)
    metrics_after = Column(JSON, nullable=True)
    realized_savings_usd = Column(Float, nullable=True)

    rollback_token = Column(String(255), nullable=True)
    rollback_executed = Column(String(10), default="no", nullable=False)

    started_at = Column(DateTime, server_default=func.now(), nullable=False)
    completed_at = Column(DateTime, nullable=True)
