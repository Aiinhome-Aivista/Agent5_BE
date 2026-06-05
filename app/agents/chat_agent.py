"""
Agent 6 — Conversational Q&A / RAG Chat Agent.

For the chat section: pulls live data across telemetry, cost, anomalies,
recommendations, action history, AND vector memory (playbooks + episodic + semantic),
then asks Mistral to produce a grounded summary with citations.

This is the user-facing "RAG chat" that summarizes everything.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from app.models.telemetry import TelemetrySnapshot, CostSnapshot, AnomalyEvent
from app.models.recommendation import Recommendation
from app.models.action_history import ActionHistory
from app.models.chat_history import ChatSession, ChatMessage
from app.services.mistral_service import mistral_service
from app.services.vector_store import get_vector_store
from app.schemas.schemas import Citation

logger = logging.getLogger(__name__)


CHAT_SYSTEM_PROMPT = """You are the PwC Platform Performance & Cost Optimization Agent — a conversational assistant for platform engineers and FinOps teams.

CAPABILITIES (you CAN):
- Explain cost trends, spikes, and anomalies grounded in the supplied telemetry & cost data
- Summarize utilization across AWS / Azure accounts
- Surface pending optimization recommendations and explain the rationale
- Cite playbooks and historical optimization runs from memory
- Answer questions about action history and rollbacks

LIMITATIONS (you CANNOT):
- Discuss topics outside platform operations, cloud cost, performance, and reliability
- Execute actions (the user must approve via the dashboard; you only describe what's possible)
- Invent metrics, costs, or resource IDs that are not in the supplied context
- Reveal credentials, secrets, or PII

RESPONSE STYLE:
- Open with a 2-3 sentence SUMMARY answering the user's question directly.
- Then provide supporting details: bullet points of concrete metrics, $ amounts, dates, resource IDs.
- Always cite which data source backed each claim using bracketed tags like [cost-2024-10-05], [rec-42], [playbook-right_size], [anomaly-17].
- If the supplied context lacks data needed to answer confidently, say so plainly.
- Be concise. Engineers value signal over fluff.

Format: plain text. No markdown headers. Use simple dashes for bullets if helpful."""


class ChatAgent:
    """Pulls live data + vector memory; produces a grounded summary."""

    def __init__(self, db: Session):
        self.db = db
        self.vs = get_vector_store()

    # ============ MAIN ENTRY ============

    def chat(
        self,
        message: str,
        session_uuid: Optional[str] = None,
        user_email: Optional[str] = None,
        provider: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        t0 = time.time()

        # 1. Resolve/create session
        session = self._get_or_create_session(session_uuid, user_email)

        # 2. Pull conversation history
        history_messages = self._build_history(session.id, max_turns=8)

        # 3. Build live data context (the "all data" the user wants summarized)
        live_context, citations = self._build_live_context(message, provider, account_id)

        # 4. Persist user message
        user_msg = ChatMessage(session_id=session.id, role="user", content=message)
        self.db.add(user_msg)
        self.db.commit()

        # 5. Call Mistral with system + context + history + user message
        full_user_content = (
            f"DATA CONTEXT (live snapshots from MySQL + vector memory):\n"
            f"{live_context}\n\n"
            f"USER QUESTION: {message}"
        )

        messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
        messages.extend(history_messages)
        messages.append({"role": "user", "content": full_user_content})

        try:
            llm_resp = mistral_service.chat(
                messages=messages, tier="frontier",
                temperature=0.3, max_tokens=1500,
            )
            answer = llm_resp["content"]
            usage = llm_resp.get("usage", {})
        except Exception as e:
            logger.exception("Mistral chat failed")
            answer = (
                "I hit an error reaching the LLM backend. The pulled data context is available, "
                f"but I couldn't produce a summary. Error: {e}"
            )
            usage = {}

        latency_ms = int((time.time() - t0) * 1000)

        # 6. Extract a short summary (first paragraph)
        summary = self._extract_summary(answer)

        # 7. Persist assistant message
        assistant_msg = ChatMessage(
            session_id=session.id,
            role="assistant",
            content=answer,
            citations=[c.model_dump() for c in citations],
            tokens_input=usage.get("prompt_tokens"),
            tokens_output=usage.get("completion_tokens"),
            latency_ms=latency_ms,
        )
        self.db.add(assistant_msg)

        # Title the session from the first user message
        if not session.title:
            session.title = message[:120]

        self.db.commit()

        return {
            "session_uuid": session.session_uuid,
            "message": answer,
            "summary": summary,
            "citations": citations,
            "latency_ms": latency_ms,
        }

    # ============ CONTEXT BUILDERS ============

    def _build_live_context(
        self, query: str, provider: Optional[str], account_id: Optional[str]
    ) -> tuple[str, List[Citation]]:
        """Pull MySQL + vector data, package as plaintext blocks for the LLM."""
        blocks: List[str] = []
        citations: List[Citation] = []

        # 1. Cost summary (last 7 vs prior 7 days)
        cost_block, cost_cites = self._cost_block(provider, account_id)
        if cost_block:
            blocks.append(cost_block)
            citations.extend(cost_cites)

        # 2. Top idle / under-utilized resources
        util_block, util_cites = self._utilization_block(provider, account_id)
        if util_block:
            blocks.append(util_block)
            citations.extend(util_cites)

        # 3. Recent anomalies
        anom_block, anom_cites = self._anomalies_block(provider, account_id)
        if anom_block:
            blocks.append(anom_block)
            citations.extend(anom_cites)

        # 4. Pending recommendations
        rec_block, rec_cites = self._recommendations_block(provider, account_id)
        if rec_block:
            blocks.append(rec_block)
            citations.extend(rec_cites)

        # 5. Recent action history
        act_block, act_cites = self._actions_block(provider, account_id)
        if act_block:
            blocks.append(act_block)
            citations.extend(act_cites)

        # 6. RAG over playbooks + episodic
        rag_block, rag_cites = self._rag_block(query)
        if rag_block:
            blocks.append(rag_block)
            citations.extend(rag_cites)

        return "\n\n".join(blocks) if blocks else "(no data available in scope)", citations

    def _cost_block(self, provider, account_id):
        q = self.db.query(CostSnapshot)
        if provider and provider != "all":
            q = q.filter(CostSnapshot.provider == provider)
        if account_id:
            q = q.filter(CostSnapshot.account_id == account_id)

        cutoff_recent = datetime.utcnow() - timedelta(days=7)
        cutoff_prev = datetime.utcnow() - timedelta(days=14)

        recent_total = (
            q.filter(CostSnapshot.usage_date >= cutoff_recent)
            .with_entities(func.sum(CostSnapshot.cost_usd))
            .scalar() or 0
        )
        prev_total = (
            q.filter(CostSnapshot.usage_date >= cutoff_prev,
                     CostSnapshot.usage_date < cutoff_recent)
            .with_entities(func.sum(CostSnapshot.cost_usd))
            .scalar() or 0
        )

        # Top 5 services in last 7 days
        top_services = (
            q.filter(CostSnapshot.usage_date >= cutoff_recent)
            .with_entities(
                CostSnapshot.service_name,
                CostSnapshot.provider,
                func.sum(CostSnapshot.cost_usd).label("total")
            )
            .group_by(CostSnapshot.service_name, CostSnapshot.provider)
            .order_by(desc("total"))
            .limit(5)
            .all()
        )

        if not recent_total and not top_services:
            return "", []

        change_pct = ((recent_total - prev_total) / prev_total * 100) if prev_total else 0
        lines = [
            "[COST OVERVIEW — last 7 days]",
            f"Total spend (last 7d): ${recent_total:.2f}",
            f"Total spend (prior 7d): ${prev_total:.2f}",
            f"Week-over-week change: {change_pct:+.1f}%",
            "Top services by spend (last 7d):",
        ]
        cites = []
        for row in top_services:
            lines.append(f"  - [{row.provider}] {row.service_name}: ${row.total:.2f}")
            cites.append(Citation(
                source=f"cost:{row.provider}:{row.service_name}",
                snippet=f"${row.total:.2f} over last 7 days",
                type="cost",
            ))
        return "\n".join(lines), cites

    def _utilization_block(self, provider, account_id):
        q = self.db.query(TelemetrySnapshot)
        if provider and provider != "all":
            q = q.filter(TelemetrySnapshot.provider == provider)
        if account_id:
            q = q.filter(TelemetrySnapshot.account_id == account_id)

        cutoff = datetime.utcnow() - timedelta(days=3)
        rows = (
            q.filter(TelemetrySnapshot.collected_at >= cutoff)
            .filter(TelemetrySnapshot.cpu_avg_pct.isnot(None))
            .order_by(TelemetrySnapshot.cpu_avg_pct.asc())
            .limit(10)
            .all()
        )
        if not rows:
            return "", []
        lines = ["[UTILIZATION — bottom 10 by avg CPU, last 3 days]"]
        cites = []
        for r in rows:
            lines.append(
                f"  - [{r.provider}/{r.resource_type}] {r.resource_name or r.resource_id} "
                f"({r.region}): avg_cpu={r.cpu_avg_pct}%, max_cpu={r.cpu_max_pct}%"
            )
            cites.append(Citation(
                source=f"telemetry:{r.resource_id}",
                snippet=f"avg_cpu={r.cpu_avg_pct}%",
                type="telemetry",
            ))
        return "\n".join(lines), cites

    def _anomalies_block(self, provider, account_id):
        q = self.db.query(AnomalyEvent).filter(AnomalyEvent.resolved_at.is_(None))
        if provider and provider != "all":
            q = q.filter(AnomalyEvent.provider == provider)
        if account_id:
            q = q.filter(AnomalyEvent.account_id == account_id)
        rows = q.order_by(AnomalyEvent.detected_at.desc()).limit(10).all()
        if not rows:
            return "", []
        lines = ["[OPEN ANOMALIES — latest 10]"]
        cites = []
        for r in rows:
            lines.append(
                f"  - [anomaly-{r.id}] {r.severity.upper()}: {r.title} "
                f"(detected {r.detected_at.date()})"
            )
            cites.append(Citation(
                source=f"anomaly-{r.id}",
                snippet=r.title,
                type="anomaly",
            ))
        return "\n".join(lines), cites

    def _recommendations_block(self, provider, account_id):
        q = self.db.query(Recommendation).filter(
            Recommendation.status.in_(["pending", "approved"])
        )
        if provider and provider != "all":
            q = q.filter(Recommendation.provider == provider)
        if account_id:
            q = q.filter(Recommendation.account_id == account_id)
        rows = q.order_by(Recommendation.estimated_monthly_savings_usd.desc()).limit(10).all()
        if not rows:
            return "", []
        total_savings = sum(r.estimated_monthly_savings_usd for r in rows)
        lines = [
            f"[PENDING RECOMMENDATIONS — top 10, potential ${total_savings:.2f}/mo savings]"
        ]
        cites = []
        for r in rows:
            lines.append(
                f"  - [rec-{r.id}] {r.risk_class.upper()} risk | "
                f"~${r.estimated_monthly_savings_usd:.2f}/mo | {r.title}"
            )
            cites.append(Citation(
                source=f"rec-{r.id}",
                snippet=f"{r.title} — ${r.estimated_monthly_savings_usd:.2f}/mo",
                type="recommendation",
            ))
        return "\n".join(lines), cites

    def _actions_block(self, provider, account_id):
        q = self.db.query(ActionHistory)
        if provider and provider != "all":
            q = q.filter(ActionHistory.provider == provider)
        if account_id:
            q = q.filter(ActionHistory.account_id == account_id)
        rows = q.order_by(ActionHistory.started_at.desc()).limit(5).all()
        if not rows:
            return "", []
        lines = ["[RECENT ACTIONS — latest 5]"]
        cites = []
        for r in rows:
            lines.append(
                f"  - [action-{r.id}] {r.status.upper()} | {r.action_type} on "
                f"{r.resource_id or '(none)'} at {r.started_at}"
            )
            cites.append(Citation(
                source=f"action-{r.id}",
                snippet=f"{r.action_type} — {r.status}",
                type="action",
            ))
        return "\n".join(lines), cites

    def _rag_block(self, query: str):
        """Retrieve from playbooks, episodic, semantic memory."""
        try:
            hits = self.vs.hybrid_query(query, n_results=6)
        except Exception as e:
            logger.warning(f"Vector query failed: {e}")
            return "", []

        lines: List[str] = []
        cites: List[Citation] = []

        if hits.get("playbooks"):
            lines.append("[PLAYBOOKS — retrieved via RAG]")
            for p in hits["playbooks"]:
                title = (p.get("metadata") or {}).get("title", "playbook")
                lines.append(f"  - [playbook:{title}] {p['text'][:280]}...")
                cites.append(Citation(
                    source=f"playbook:{title}",
                    snippet=p["text"][:200],
                    type="playbook",
                ))

        if hits.get("episodic"):
            lines.append("\n[PAST OPTIMIZATION RUNS — episodic memory]")
            for e in hits["episodic"]:
                lines.append(f"  - {e['text'][:280]}...")
                cites.append(Citation(
                    source=f"episodic:{(e.get('metadata') or {}).get('recommendation_id','')}",
                    snippet=e["text"][:200],
                    type="episodic",
                ))

        if hits.get("semantic"):
            lines.append("\n[DISTILLED LEARNINGS — semantic memory]")
            for s in hits["semantic"]:
                lines.append(f"  - {s['text'][:280]}...")
                cites.append(Citation(
                    source="semantic-memory",
                    snippet=s["text"][:200],
                    type="semantic",
                ))

        return "\n".join(lines), cites

    # ============ HISTORY ============

    def _get_or_create_session(self, session_uuid: Optional[str],
                               user_email: Optional[str]) -> ChatSession:
        if session_uuid:
            existing = self.db.query(ChatSession).filter_by(session_uuid=session_uuid).first()
            if existing:
                return existing
        session = ChatSession(
            session_uuid=session_uuid or str(uuid.uuid4()),
            user_email=user_email,
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def _build_history(self, session_id: int, max_turns: int = 8) -> List[Dict[str, str]]:
        rows = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.desc())
            .limit(max_turns * 2)
            .all()
        )
        rows = list(reversed(rows))
        return [{"role": r.role, "content": r.content} for r in rows
                if r.role in ("user", "assistant")]

    @staticmethod
    def _extract_summary(answer: str) -> str:
        # First non-empty paragraph
        for chunk in answer.split("\n\n"):
            if chunk.strip():
                return chunk.strip()[:500]
        return answer[:300]
