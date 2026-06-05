"""
Agent 1 — Orchestrator (Manager).

Runs the Observe → Reason → Plan → Act → Evaluate loop.
Writes per-step progress into a ScanRun row so the UI can poll status.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.agents.telemetry_agent import TelemetryAgent
from app.agents.analyzer_agent import AnalyzerAgent
from app.agents.action_agent import ActionExecutorAgent
from app.models.scan_run import ScanRun

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, db: Session):
        self.db = db

    def _new_run(self, provider: str, dry_run: bool, trigger: str = "manual") -> ScanRun:
        run = ScanRun(
            provider=provider,
            dry_run=dry_run,
            trigger=trigger,
            status="running",
            current_step="starting",
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def _update_run(self, run: ScanRun, **fields):
        for k, v in fields.items():
            setattr(run, k, v)
        self.db.commit()

    def run_scan(
        self,
        provider: str = "all",
        dry_run: bool = True,
        trigger: str = "manual",
    ) -> Dict[str, Any]:
        t0 = time.time()
        run = self._new_run(provider=provider, dry_run=dry_run, trigger=trigger)
        report: Dict[str, Any] = {
            "scan_run_id": run.id,
            "provider": provider,
            "telemetry": {},
            "anomalies": 0,
            "recommendations": 0,
            "auto_executed": 0,
            "errors": [],
        }

        telemetry_agent = TelemetryAgent(self.db)
        analyzer = AnalyzerAgent(self.db)
        executor = ActionExecutorAgent(self.db)

        # 1. OBSERVE — pull telemetry
        self._update_run(run, current_step="collecting_telemetry")
        scanned = 0
        if provider in ("aws", "all"):
            try:
                aws_res = telemetry_agent.collect_aws_telemetry()
                report["telemetry"]["aws"] = aws_res
                scanned += aws_res.get("scanned_resources", 0)
            except Exception as e:
                logger.exception("AWS telemetry failed")
                report["errors"].append(f"aws_telemetry: {e}")
        if provider in ("azure", "all"):
            try:
                az_res = telemetry_agent.collect_azure_telemetry()
                report["telemetry"]["azure"] = az_res
                scanned += az_res.get("scanned_resources", 0)
            except Exception as e:
                logger.exception("Azure telemetry failed")
                report["errors"].append(f"azure_telemetry: {e}")
        self._update_run(run, scanned_resources=scanned)

        # 2. REASON — detect anomalies
        self._update_run(run, current_step="detecting_anomalies")
        try:
            anoms = telemetry_agent.detect_anomalies(
                provider=None if provider == "all" else provider
            )
            report["anomalies"] = len(anoms)
            self._update_run(run, anomalies_found=len(anoms))
        except Exception as e:
            logger.exception("Anomaly detection failed")
            report["errors"].append(f"anomaly_detection: {e}")

        # 3. PLAN — produce recommendations via LLM + RAG
        self._update_run(run, current_step="analyzing")
        try:
            recs = analyzer.analyze_all_open_anomalies(limit=20)
            report["recommendations"] = len(recs)
            self._update_run(run, recommendations_created=len(recs))
        except Exception as e:
            logger.exception("Recommendation generation failed")
            report["errors"].append(f"analyzer: {e}")
            recs = []

        # 4. ACT — auto-execute LOW-risk recommendations (if enabled)
        if settings.AUTO_EXECUTE_LOW_RISK and not dry_run:
            self._update_run(run, current_step="auto_executing")
            for rec in recs:
                if rec.risk_class != "low":
                    continue
                try:
                    executor.execute(rec.id, actor="agent", force_dry_run=False)
                    report["auto_executed"] += 1
                except Exception as e:
                    logger.warning(f"Auto-exec of rec {rec.id} skipped: {e}")
                    report["errors"].append(f"auto_exec_{rec.id}: {e}")
            self._update_run(run, actions_executed=report["auto_executed"])

        duration_ms = int((time.time() - t0) * 1000)
        report["duration_ms"] = duration_ms

        # Final state
        self._update_run(
            run,
            status="failed" if report["errors"] and scanned == 0 else "succeeded",
            current_step="done",
            duration_ms=duration_ms,
            details=report,
            finished_at=datetime.utcnow(),
            error_message="; ".join(report["errors"])[:1000] if report["errors"] else None,
        )

        return report
