"""Optimization recommendations produced by the Recommendation Agent."""
from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, Text
from sqlalchemy.sql import func
from app.database import Base


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(20), nullable=False, index=True)
    account_id = Column(String(255), nullable=False, index=True)
    resource_id = Column(String(500), nullable=True)
    resource_type = Column(String(100), nullable=True)
    resource_name = Column(String(500), nullable=True)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    action_type = Column(String(100), nullable=False)
    # right_size, stop_idle, schedule_shift, storage_tier, autosuspend, etc.

    risk_class = Column(String(20), nullable=False, index=True)  # low, medium, high
    estimated_monthly_savings_usd = Column(Float, default=0.0)
    estimated_latency_improvement_pct = Column(Float, default=0.0)
    confidence_score = Column(Float, default=0.0)

    rollback_plan = Column(Text, nullable=True)
    source_anomaly_id = Column(Integer, nullable=True)
    rationale = Column(Text, nullable=True)  # LLM rationale + citations
    payload = Column(JSON, nullable=True)  # Action params

    status = Column(
        String(30), default="pending", nullable=False, index=True
    )  # pending, approved, rejected, executed, failed, expired

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String(255), nullable=True)
    executed_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    rejected_reason = Column(String(500), nullable=True)

    @property
    def scale_suggestion(self) -> str:
        """Human-readable scale-up / scale-down guidance derived from the action."""
        payload = self.payload or {}
        atype = (self.action_type or "").lower()
        target = (
            payload.get("new_size")
            or payload.get("new_type")
            or payload.get("target_size")
        )
        if atype in ("right_size", "right_sizing", "downsize"):
            if target:
                return f"Scale down to {target}"
            return "Scale down one tier"
        if atype in ("stop_idle", "deallocate", "auto_shutdown", "idle_resource"):
            return "Scale to zero (deallocate while idle)"
        if atype in ("scale_up", "upsize"):
            return f"Scale up to {target}" if target else "Scale up one tier"
        if atype in ("storage_tier", "storage_tiering"):
            return "Scale down storage tier (move to cooler/archive tier)"
        if atype in ("autosuspend", "auto_suspend"):
            return "Scale to zero faster (shorten auto-suspend window)"
        return "Review scaling: right-size to match observed utilization"
