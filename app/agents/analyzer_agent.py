"""
Agent 3 + Agent 4 — Cost & Performance Analyzer + Recommendation Generator.

- Pulls anomaly events
- Retrieves relevant playbooks from ChromaDB (RAG)
- Calls Mistral frontier to perform RCA + propose optimization
- Persists structured Recommendation rows
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.telemetry import AnomalyEvent, TelemetrySnapshot
from app.models.recommendation import Recommendation
from app.services.mistral_service import mistral_service
from app.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)


ANALYZER_SYSTEM_PROMPT = """You are a Cloud Cost & Performance Analyzer Agent at PwC.
Your role is root-cause analysis: given a detected anomaly and retrieved playbooks,
produce a structured optimization recommendation in strict JSON.

CAPABILITIES:
- You CAN analyze telemetry, costs, and playbooks to recommend optimizations.
- You CANNOT execute actions; you only propose them.
- You CANNOT touch out-of-scope resources.

OUTPUT — STRICT JSON SCHEMA, no markdown, no commentary:
{
  "title": "Short action title (max 100 chars)",
  "description": "1-3 sentence description of the issue and proposed fix",
  "action_type": "right_size | stop_idle | schedule_shift | storage_tier | autosuspend | other",
  "risk_class": "low | medium | high",
  "estimated_monthly_savings_usd": <number>,
  "estimated_latency_improvement_pct": <number, 0 if N/A>,
  "confidence_score": <0.0-1.0>,
  "rollback_plan": "Concrete steps to revert if action fails",
  "rationale": "Why this action — cite specific metrics and playbook references",
  "payload": { "...action-specific params (resource_id, region, new_type, etc.)" }
}

RULES:
- Be conservative on savings estimates. Cite the metric values you used.
- risk_class HIGH if the action touches prod, modifies schemas, or is irreversible.
- risk_class LOW only for dev/test idle stops or storage tier moves with built-in lifecycle.
- If insufficient evidence, set confidence_score < 0.5 and explain in rationale.
"""


class AnalyzerAgent:
    def __init__(self, db: Session):
        self.db = db
        self.vs = get_vector_store()

    def analyze_anomaly(self, anomaly: AnomalyEvent) -> Optional[Recommendation]:
        # 1. Build RAG context — query playbooks
        rag_query = self._build_rag_query(anomaly)
        playbook_hits = self.vs.query_playbooks(rag_query, n_results=4, where={
            "provider": {"$in": [anomaly.provider, "any"]}
        } if anomaly.provider else None)

        episodic_hits = self.vs.query_episodic(rag_query, n_results=2)

        # 2. Gather supporting telemetry (latest snapshot for this resource if any)
        telemetry_ctx = ""
        if anomaly.resource_id:
            snap = (
                self.db.query(TelemetrySnapshot)
                .filter(TelemetrySnapshot.resource_id == anomaly.resource_id)
                .order_by(TelemetrySnapshot.window_end.desc())
                .first()
            )
            if snap:
                telemetry_ctx = (
                    f"Latest telemetry for {snap.resource_id}: "
                    f"cpu_avg={snap.cpu_avg_pct}, cpu_max={snap.cpu_max_pct}, "
                    f"resource_type={snap.resource_type}, region={snap.region}, "
                    f"extra={json.dumps(snap.extra_metrics or {}, default=str)[:500]}"
                )

        # 3. Build messages for Mistral
        playbooks_block = "\n\n".join(
            f"[Playbook: {p['metadata'].get('title','')}] {p['text']}"
            for p in playbook_hits
        ) or "(no relevant playbooks retrieved)"

        episodic_block = "\n\n".join(
            f"[Past run: {e['metadata'].get('outcome','')}] {e['text']}"
            for e in episodic_hits
        ) or "(no relevant past runs)"

        user_prompt = f"""Anomaly to analyze:
- Type: {anomaly.anomaly_type}
- Severity: {anomaly.severity}
- Provider: {anomaly.provider}
- Account: {anomaly.account_id}
- Resource: {anomaly.resource_id} ({anomaly.resource_type})
- Title: {anomaly.title}
- Description: {anomaly.description}
- Metric value: {anomaly.metric_value}
- Baseline: {anomaly.baseline_value}
- Deviation: {anomaly.deviation_pct}
- Details: {json.dumps(anomaly.details or {}, default=str)}

Telemetry context:
{telemetry_ctx or "(no current telemetry snapshot)"}

Retrieved playbooks (RAG):
{playbooks_block}

Past optimization outcomes (episodic memory):
{episodic_block}

Produce the recommendation JSON object now."""

        messages = [
            {"role": "system", "content": ANALYZER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            parsed = mistral_service.chat_json(messages, tier="frontier", temperature=0.1)
        except Exception as e:
            logger.exception(f"Mistral analyzer call failed: {e}")
            return None

        if parsed.get("_error"):
            logger.error(f"Analyzer JSON parse failed: {parsed}")
            return None

        # 4. Validate and persist Recommendation
        rec = Recommendation(
            provider=anomaly.provider,
            account_id=anomaly.account_id,
            resource_id=anomaly.resource_id,
            resource_type=anomaly.resource_type,
            resource_name=anomaly.resource_id,
            title=str(parsed.get("title", "Optimization recommendation"))[:500],
            description=str(parsed.get("description", "")),
            action_type=str(parsed.get("action_type", "other"))[:100],
            risk_class=self._validate_risk(parsed.get("risk_class", "medium")),
            estimated_monthly_savings_usd=float(parsed.get("estimated_monthly_savings_usd", 0) or 0),
            estimated_latency_improvement_pct=float(parsed.get("estimated_latency_improvement_pct", 0) or 0),
            confidence_score=float(parsed.get("confidence_score", 0.5) or 0.5),
            rollback_plan=str(parsed.get("rollback_plan", "")),
            rationale=str(parsed.get("rationale", "")),
            payload=parsed.get("payload") or {},
            source_anomaly_id=anomaly.id,
            status="pending",
        )
        self.db.add(rec)
        self.db.commit()
        self.db.refresh(rec)
        logger.info(f"Recommendation {rec.id} created for anomaly {anomaly.id}")
        return rec

    def analyze_all_open_anomalies(self, limit: int = 50) -> List[Recommendation]:
        # Skip anomalies that already have a recommendation
        anomalies = (
            self.db.query(AnomalyEvent)
            .filter(AnomalyEvent.resolved_at.is_(None))
            .order_by(AnomalyEvent.detected_at.desc())
            .limit(limit)
            .all()
        )
        produced: List[Recommendation] = []
        for a in anomalies:
            existing = (
                self.db.query(Recommendation)
                .filter(Recommendation.source_anomaly_id == a.id)
                .first()
            )
            if existing:
                continue
            rec = self.analyze_anomaly(a)
            if rec:
                produced.append(rec)
        return produced

    @staticmethod
    def _build_rag_query(anomaly: AnomalyEvent) -> str:
        parts = [anomaly.anomaly_type, anomaly.provider]
        if anomaly.resource_type:
            parts.append(anomaly.resource_type)
        parts.append(anomaly.title or "")
        return " ".join(p for p in parts if p)

    @staticmethod
    def _validate_risk(v: str) -> str:
        v = (v or "").lower().strip()
        return v if v in ("low", "medium", "high") else "medium"
