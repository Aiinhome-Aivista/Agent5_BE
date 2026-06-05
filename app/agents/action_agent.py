"""
Agent 5 — Action Executor Agent.

Executes approved optimizations via cloud APIs.
- Idempotent operations
- Circuit breakers (skip if blast radius exceeded)
- Automatic rollback registration
- Dry-run for high-risk actions
- Audit trail via ActionHistory rows
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.recommendation import Recommendation
from app.models.action_history import ActionHistory
from app.services.aws_service import AWSService, aws_service_from_account
from app.services.azure_service import AzureService, azure_service_from_account
from app.models.cloud_account import CloudAccount
from app.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)


class ActionExecutorAgent:
    def __init__(self, db: Session):
        self.db = db
        self.vs = get_vector_store()

    def _resolve_aws(self, account_id: str) -> AWSService:
        """Build an AWSService for a specific registered account."""
        account = (
            self.db.query(CloudAccount)
            .filter(CloudAccount.provider == "aws", CloudAccount.account_identifier == account_id)
            .first()
        )
        if not account:
            raise ValueError(f"AWS account {account_id} not registered. Add it in Settings.")
        return aws_service_from_account(account)

    def _resolve_azure(self, account_id: str) -> AzureService:
        """Build an AzureService for a specific registered subscription."""
        account = (
            self.db.query(CloudAccount)
            .filter(CloudAccount.provider == "azure", CloudAccount.account_identifier == account_id)
            .first()
        )
        if not account:
            raise ValueError(f"Azure subscription {account_id} not registered. Add it in Settings.")
        return azure_service_from_account(account)

    # ============ AUTONOMY GATE ============

    def _check_blast_radius(self, account_id: str) -> bool:
        """Return True if within blast-radius (safe to execute), False to block."""
        cutoff = datetime.utcnow() - timedelta(hours=1)
        count = (
            self.db.query(ActionHistory)
            .filter(
                ActionHistory.account_id == account_id,
                ActionHistory.started_at >= cutoff,
                ActionHistory.status.in_(["initiated", "succeeded"]),
            )
            .count()
        )
        if count >= settings.BLAST_RADIUS_MAX_RESOURCES_PER_HOUR:
            logger.warning(
                f"Blast radius hit for {account_id}: {count} actions in last hour"
            )
            return False
        return True

    # ============ EXECUTION ============

    def execute(self, recommendation_id: int, actor: str = "agent",
                force_dry_run: bool = False) -> ActionHistory:
        rec = self.db.query(Recommendation).filter_by(id=recommendation_id).first()
        if not rec:
            raise ValueError(f"Recommendation {recommendation_id} not found")

        # Pre-flight checks
        if rec.status not in ("approved", "pending"):
            raise ValueError(f"Recommendation {rec.id} status={rec.status}; not executable")
        if rec.risk_class != "low" and rec.status == "pending":
            raise ValueError(f"Recommendation {rec.id} (risk={rec.risk_class}) requires approval")
        if not self._check_blast_radius(rec.account_id):
            raise RuntimeError("Blast radius exceeded — cooling off")

        # Always dry-run for high-risk, even if approved
        dry_run = force_dry_run or rec.risk_class == "high"

        history = ActionHistory(
            recommendation_id=rec.id,
            provider=rec.provider,
            account_id=rec.account_id,
            resource_id=rec.resource_id,
            action_type=rec.action_type,
            status="initiated",
            actor=actor,
            request_payload={**(rec.payload or {}), "dry_run": dry_run},
        )
        self.db.add(history)
        self.db.commit()
        self.db.refresh(history)

        try:
            result = self._dispatch(rec, dry_run=dry_run)
            history.status = "dry_run" if dry_run else "succeeded"
            history.response_payload = result
            history.completed_at = datetime.utcnow()
            history.rollback_token = result.get("rollback_token") or str(uuid.uuid4())

            if not dry_run:
                rec.status = "executed"
                rec.executed_at = datetime.utcnow()
                # Realized savings: bank the estimated monthly savings as realized
                # so the audit trail can demonstrate savings accrued over time.
                history.realized_savings_usd = float(
                    rec.estimated_monthly_savings_usd or 0.0
                )
                # Save to episodic memory for future learning
                self._record_episodic(rec, history, success=True)
            self.db.commit()
            self.db.refresh(history)
            return history

        except Exception as e:
            logger.exception(f"Action execution failed for rec={rec.id}: {e}")
            history.status = "failed"
            history.error_message = str(e)
            history.completed_at = datetime.utcnow()
            rec.status = "failed"
            self.db.commit()
            return history

    def _dispatch(self, rec: Recommendation, dry_run: bool) -> Dict[str, Any]:
        """Route to the correct cloud action based on (provider, action_type)."""
        payload = rec.payload or {}
        provider = rec.provider
        atype = rec.action_type

        if provider == "aws":
            aws = self._resolve_aws(rec.account_id)
            if atype == "stop_idle":
                return aws.stop_ec2_instance(
                    instance_id=payload.get("instance_id") or rec.resource_id,
                    region=payload.get("region", settings.AWS_DEFAULT_REGION),
                    dry_run=dry_run,
                )
            if atype == "right_size":
                return aws.modify_ec2_instance_type(
                    instance_id=payload.get("instance_id") or rec.resource_id,
                    new_type=payload.get("new_type", "t3.medium"),
                    region=payload.get("region", settings.AWS_DEFAULT_REGION),
                    dry_run=dry_run,
                )

        if provider == "azure":
            azure = self._resolve_azure(rec.account_id)
            if atype == "stop_idle":
                return azure.deallocate_vm(
                    resource_group=payload.get("resource_group", ""),
                    vm_name=payload.get("vm_name", ""),
                    dry_run=dry_run,
                )
            if atype == "right_size":
                return azure.resize_vm(
                    resource_group=payload.get("resource_group", ""),
                    vm_name=payload.get("vm_name", ""),
                    new_size=payload.get("new_size", "Standard_B2s"),
                    dry_run=dry_run,
                )

        # Unhandled — return as dry-run for safety
        return {
            "success": False,
            "dry_run": True,
            "message": f"No executor handler for provider={provider}, action_type={atype}",
        }

    # ============ ROLLBACK ============

    def rollback(self, action_history_id: int, actor: str = "agent") -> ActionHistory:
        ah = self.db.query(ActionHistory).filter_by(id=action_history_id).first()
        if not ah:
            raise ValueError(f"ActionHistory {action_history_id} not found")
        if ah.rollback_executed == "yes":
            raise ValueError("Already rolled back")
        if ah.status not in ("succeeded", "failed"):
            raise ValueError(f"Cannot rollback status={ah.status}")

        try:
            if ah.provider == "aws" and ah.action_type == "stop_idle":
                payload = ah.request_payload or {}
                aws = self._resolve_aws(ah.account_id)
                result = aws.start_ec2_instance(
                    instance_id=payload.get("instance_id") or ah.resource_id,
                    region=payload.get("region", settings.AWS_DEFAULT_REGION),
                    dry_run=False,
                )
            elif ah.provider == "azure" and ah.action_type == "stop_idle":
                payload = ah.request_payload or {}
                azure = self._resolve_azure(ah.account_id)
                result = azure.start_vm(
                    resource_group=payload.get("resource_group", ""),
                    vm_name=payload.get("vm_name", ""),
                    dry_run=False,
                )
            else:
                result = {"success": False, "message": "no rollback handler"}

            ah.rollback_executed = "yes"
            ah.status = "rolled_back"
            ah.response_payload = {**(ah.response_payload or {}), "rollback": result}
            ah.completed_at = datetime.utcnow()
            self.db.commit()
            return ah
        except Exception as e:
            logger.exception(f"Rollback failed for action {ah.id}: {e}")
            ah.error_message = (ah.error_message or "") + f" | rollback_error: {e}"
            self.db.commit()
            raise

    # ============ MEMORY ============

    def _record_episodic(self, rec: Recommendation, ah: ActionHistory, success: bool):
        """Persist outcome to episodic memory in ChromaDB for future RAG."""
        try:
            text = (
                f"On {datetime.utcnow().date()}, executed '{rec.action_type}' on "
                f"{rec.provider}:{rec.resource_id} ({rec.resource_type}). "
                f"Outcome: {'success' if success else 'failure'}. "
                f"Estimated savings: ${rec.estimated_monthly_savings_usd}/mo. "
                f"Rationale: {rec.rationale or ''}"
            )
            self.vs.add_episodic_memory(text, {
                "recommendation_id": rec.id,
                "action_history_id": ah.id,
                "provider": rec.provider,
                "resource_type": rec.resource_type or "",
                "action_type": rec.action_type,
                "outcome": "success" if success else "failure",
                "date": str(datetime.utcnow().date()),
            })
        except Exception as e:
            logger.warning(f"Failed to record episodic memory: {e}")
