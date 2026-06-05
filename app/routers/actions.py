"""Action history router — list executed actions, rollback support."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional
from loguru import logger

from app.database import get_db
from app.models.action_history import ActionHistory
from app.models.recommendation import Recommendation
from app.agents.action_agent import ActionExecutorAgent

router = APIRouter(prefix="/api/actions", tags=["actions"])


def _serialize(a: ActionHistory, full: bool = False) -> dict:
    base = {
        "id": a.id,
        "recommendation_id": a.recommendation_id,
        "provider": a.provider,
        "account_id": a.account_id,
        "status": a.status,
        "actor": a.actor,
        "action_type": a.action_type,
        "resource_id": a.resource_id,
        "realized_savings_usd": float(a.realized_savings_usd) if a.realized_savings_usd else None,
        "rollback_executed": a.rollback_executed,
        "rollback_token": a.rollback_token,
        "started_at": a.started_at,
        "completed_at": a.completed_at,
        "error_message": a.error_message,
    }
    if full:
        base.update({
            "request_payload": a.request_payload,
            "response_payload": a.response_payload,
            "metrics_before": a.metrics_before,
            "metrics_after": a.metrics_after,
        })
    return base


@router.get("")
def list_actions(
    status: Optional[str] = None,
    actor: Optional[str] = None,
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    """List action history with optional filters."""
    q = db.query(ActionHistory)
    if status:
        q = q.filter(ActionHistory.status == status)
    if actor:
        q = q.filter(ActionHistory.actor == actor)
    actions = q.order_by(ActionHistory.started_at.desc()).limit(limit).all()
    return [_serialize(a) for a in actions]


@router.get("/{action_id}")
def get_action(action_id: int, db: Session = Depends(get_db)):
    """Get a single action history record with full payload."""
    action = db.query(ActionHistory).filter(ActionHistory.id == action_id).first()
    if not action:
        raise HTTPException(status_code=404, detail="Action not found")
    return _serialize(action, full=True)


@router.post("/{action_id}/rollback")
def rollback_action(action_id: int, actor: str = "ui-user", db: Session = Depends(get_db)):
    """Roll back a successful action (reverses the operation)."""
    try:
        agent = ActionExecutorAgent(db)
        result = agent.rollback(action_id, actor=actor)
        return _serialize(result, full=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Rollback failed: {}", e)
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")
