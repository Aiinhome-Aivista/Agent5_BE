# """Recommendation endpoints — list, approve, reject, execute."""
# from datetime import datetime
# from typing import List, Optional

# from fastapi import APIRouter, Depends, HTTPException
# from sqlalchemy.orm import Session

# from app.database import get_db
# from app.models.recommendation import Recommendation
# from app.agents.action_agent import ActionExecutorAgent
# from app.schemas.schemas import RecommendationOut, RecommendationDecision, ActionHistoryOut

# router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


# @router.get("", response_model=List[RecommendationOut])
# def list_recommendations(
#     status: Optional[str] = None,
#     provider: Optional[str] = None,
#     account_id: Optional[str] = None,
#     risk_class: Optional[str] = None,
#     limit: int = 200,
#     db: Session = Depends(get_db),
# ):
#     q = db.query(Recommendation)
#     if status:
#         q = q.filter(Recommendation.status == status)
#     if provider and provider != "all":
#         q = q.filter(Recommendation.provider == provider)
#     if account_id:
#         q = q.filter(Recommendation.account_id == account_id)
#     if risk_class:
#         q = q.filter(Recommendation.risk_class == risk_class)
#     return q.order_by(
#         Recommendation.estimated_monthly_savings_usd.desc()
#     ).limit(limit).all()


# @router.get("/{rec_id}", response_model=RecommendationOut)
# def get_recommendation(rec_id: int, db: Session = Depends(get_db)):
#     rec = db.query(Recommendation).filter_by(id=rec_id).first()
#     if not rec:
#         raise HTTPException(404, "Recommendation not found")
#     return rec


# @router.post("/{rec_id}/decision", response_model=RecommendationOut)
# def decide_recommendation(
#     rec_id: int, decision: RecommendationDecision, db: Session = Depends(get_db)
# ):
#     """Approve or reject a recommendation."""
#     rec = db.query(Recommendation).filter_by(id=rec_id).first()
#     if not rec:
#         raise HTTPException(404, "Recommendation not found")
#     if rec.status not in ("pending",):
#         raise HTTPException(400, f"Cannot decide on rec in status={rec.status}")

#     if decision.decision == "approve":
#         rec.status = "approved"
#         rec.approved_at = datetime.utcnow()
#         rec.approved_by = decision.user_email
#     elif decision.decision == "reject":
#         rec.status = "rejected"
#         rec.rejected_at = datetime.utcnow()
#         rec.rejected_reason = decision.reason
#     else:
#         raise HTTPException(400, "decision must be 'approve' or 'reject'")

#     db.commit()
#     db.refresh(rec)
#     return rec


# @router.post("/{rec_id}/execute", response_model=ActionHistoryOut)
# def execute_recommendation(
#     rec_id: int,
#     actor: Optional[str] = "user",
#     force_dry_run: bool = False,
#     db: Session = Depends(get_db),
# ):
#     """Execute (or dry-run) an approved recommendation."""
#     rec = db.query(Recommendation).filter_by(id=rec_id).first()
#     if not rec:
#         raise HTTPException(404, "Recommendation not found")
#     executor = ActionExecutorAgent(db)
#     try:
#         ah = executor.execute(rec.id, actor=actor or "user", force_dry_run=force_dry_run)
#         return ah
#     except (ValueError, RuntimeError) as e:
#         raise HTTPException(400, str(e))



"""Recommendation endpoints — list, approve, reject, execute."""

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models.recommendation import Recommendation
from app.models.action_history import ActionHistory
from app.models.telemetry import CostSnapshot
from app.agents.action_agent import ActionExecutorAgent
from app.schemas.schemas import (
    RecommendationOut,
    RecommendationDecision,
    ActionHistoryOut,
)

router = APIRouter(
    prefix="/api/recommendations",
    tags=["recommendations"]
)

# Dangerous actions that should NEVER auto execute
BLOCKED_ACTIONS = [
    "delete_service",
    "delete_database",
    "terminate_cluster",
    "destroy_vm",
]


@router.get("", response_model=List[RecommendationOut])
def list_recommendations(
    status: Optional[str] = None,
    provider: Optional[str] = None,
    account_id: Optional[str] = None,
    risk_class: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    q = db.query(Recommendation)

    if status:
        q = q.filter(Recommendation.status == status)

    if provider and provider != "all":
        q = q.filter(Recommendation.provider == provider)

    if account_id:
        q = q.filter(Recommendation.account_id == account_id)

    if risk_class:
        q = q.filter(Recommendation.risk_class == risk_class)

    return (
        q.order_by(
            Recommendation.estimated_monthly_savings_usd.desc()
        )
        .limit(limit)
        .all()
    )


@router.get("/summary/savings")
def savings_summary(
    provider: Optional[str] = None,
    account_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Aggregated savings view for the dashboard / recommendations header.

    Returns current monthly spend, projected spend if all open recommendations
    are applied, the total aggregated benefit, and realized savings to date
    (from the audit trail). All values are in USD.
    """
    cutoff = datetime.utcnow() - timedelta(days=30)

    sub = (
        db.query(
            CostSnapshot.service_name,
            CostSnapshot.provider,
            CostSnapshot.usage_date,
            func.max(CostSnapshot.id).label("latest_id"),
        )
        .filter(CostSnapshot.usage_date >= cutoff)
    )
    if provider and provider != "all":
        sub = sub.filter(CostSnapshot.provider == provider)
    sub = sub.group_by(
        CostSnapshot.service_name,
        CostSnapshot.provider,
        CostSnapshot.usage_date,
    ).subquery()

    current_spend = (
        db.query(func.sum(CostSnapshot.cost_usd))
        .join(sub, CostSnapshot.id == sub.c.latest_id)
        .scalar()
        or 0.0
    )

    def _sum(statuses):
        q = db.query(func.sum(Recommendation.estimated_monthly_savings_usd)).filter(
            Recommendation.status.in_(statuses)
        )
        if provider and provider != "all":
            q = q.filter(Recommendation.provider == provider)
        if account_id:
            q = q.filter(Recommendation.account_id == account_id)
        return float(q.scalar() or 0.0)

    def _count(statuses):
        q = db.query(func.count(Recommendation.id)).filter(
            Recommendation.status.in_(statuses)
        )
        if provider and provider != "all":
            q = q.filter(Recommendation.provider == provider)
        if account_id:
            q = q.filter(Recommendation.account_id == account_id)
        return int(q.scalar() or 0)

    open_savings = _sum(["pending", "approved"])
    executed_savings = _sum(["executed"])
    total_benefit = open_savings + executed_savings

    rq = db.query(func.sum(ActionHistory.realized_savings_usd)).filter(
        ActionHistory.status == "succeeded"
    )
    if provider and provider != "all":
        rq = rq.filter(ActionHistory.provider == provider)
    if account_id:
        rq = rq.filter(ActionHistory.account_id == account_id)
    realized_to_date = float(rq.scalar() or 0.0)

    current_spend = float(current_spend)
    projected_spend = max(current_spend - total_benefit, 0.0)
    pct = (total_benefit / current_spend * 100.0) if current_spend > 0 else 0.0

    return {
        "currency": "USD",
        "current_monthly_spend_usd": round(current_spend, 2),
        "projected_monthly_spend_usd": round(projected_spend, 2),
        "open_recommendation_savings_usd": round(open_savings, 2),
        "executed_savings_usd": round(executed_savings, 2),
        "total_aggregated_benefit_usd": round(total_benefit, 2),
        "realized_savings_to_date_usd": round(realized_to_date, 2),
        "savings_pct_of_spend": round(pct, 1),
        "open_recommendation_count": _count(["pending", "approved"]),
        "executed_recommendation_count": _count(["executed"]),
    }


@router.get("/{rec_id}", response_model=RecommendationOut)
def get_recommendation(
    rec_id: int,
    db: Session = Depends(get_db)
):
    rec = db.query(Recommendation).filter_by(id=rec_id).first()

    if not rec:
        raise HTTPException(
            status_code=404,
            detail="Recommendation not found"
        )

    return rec


@router.post("/{rec_id}/decision", response_model=RecommendationOut)
def decide_recommendation(
    rec_id: int,
    decision: RecommendationDecision,
    db: Session = Depends(get_db),
):
    """
    Human approval/rejection endpoint.
    Automated approvals are NOT allowed.
    """

    rec = db.query(Recommendation).filter_by(id=rec_id).first()

    if not rec:
        raise HTTPException(
            status_code=404,
            detail="Recommendation not found"
        )

    # Only pending recommendations can be reviewed
    if rec.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot decide on recommendation in status={rec.status}"
        )

    # Prevent automated/system approvals
    blocked_approvers = ["system", "auto", "bot", "agent", "ai"]

    if (
        not decision.user_email
        or decision.user_email.lower() in blocked_approvers
    ):
        raise HTTPException(
            status_code=403,
            detail="Human approval is required"
        )

    # APPROVE
    if decision.decision == "approve":

        # Prevent automatic approval of high-risk actions
        if rec.risk_class == "high":
            raise HTTPException(
                status_code=403,
                detail="High-risk recommendations require admin review"
            )

        rec.status = "approved"
        rec.approved_at = datetime.utcnow()
        rec.approved_by = decision.user_email

    # REJECT
    elif decision.decision == "reject":

        rec.status = "rejected"
        rec.rejected_at = datetime.utcnow()
        rec.rejected_reason = decision.reason

    else:
        raise HTTPException(
            status_code=400,
            detail="decision must be 'approve' or 'reject'"
        )

    db.commit()
    db.refresh(rec)

    return rec


@router.post("/{rec_id}/execute", response_model=ActionHistoryOut)
def execute_recommendation(
    rec_id: int,
    actor: Optional[str] = "user",
    force_dry_run: bool = False,
    db: Session = Depends(get_db),
):
    """
    Execute approved recommendations only.

    Strict Guardrails:
    - Human approval required
    - No automated approvals
    - High-risk execution blocked
    - Dangerous delete actions blocked
    """

    rec = db.query(Recommendation).filter_by(id=rec_id).first()

    if not rec:
        raise HTTPException(
            status_code=404,
            detail="Recommendation not found"
        )

    # Must be approved first
    if rec.status != "approved":
        raise HTTPException(
            status_code=403,
            detail="Recommendation must be manually approved before execution"
        )

    # Human approver required
    if not rec.approved_by:
        raise HTTPException(
            status_code=403,
            detail="Human approval missing"
        )

    # Prevent bot/system execution approvals
    blocked_approvers = ["system", "auto", "bot", "agent", "ai"]

    if rec.approved_by.lower() in blocked_approvers:
        raise HTTPException(
            status_code=403,
            detail="Automated approvals are forbidden"
        )

    # Block dangerous high-risk execution
    if rec.risk_class == "high":
        raise HTTPException(
            status_code=403,
            detail="High-risk recommendations cannot be executed automatically"
        )

    # Block destructive actions
    action_type = getattr(rec, "action_type", None)

    if action_type in BLOCKED_ACTIONS:
        raise HTTPException(
            status_code=403,
            detail=f"Dangerous action blocked: {action_type}"
        )

    executor = ActionExecutorAgent(db)

    try:
        ah = executor.execute(
            rec.id,
            actor=actor or "user",
            force_dry_run=force_dry_run
        )

        return ah

    except (ValueError, RuntimeError) as e:
        raise HTTPException(
            status_code=400,
            detail=str(e)
        )
