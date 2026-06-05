"""Scan / Orchestrator endpoints — kick off the agent pipeline."""
from typing import Optional, Literal
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.database import get_db
from app.agents.orchestrator import Orchestrator
from app.schemas.schemas import ScanResponse, StatusResponse
from app.models.scan_run import ScanRun

router = APIRouter(prefix="/api/scan", tags=["scan"])


class ScanBody(BaseModel):
    """Body for POST /scan/run. All fields optional with sensible defaults."""
    provider: Literal["aws", "azure", "all"] = "all"
    dry_run: bool = False


def _run_dict(r: ScanRun) -> dict:
    return {
        "id": r.id,
        "provider": r.provider,
        "trigger": r.trigger,
        "dry_run": r.dry_run,
        "status": r.status,
        "current_step": r.current_step,
        "scanned_resources": r.scanned_resources,
        "anomalies_found": r.anomalies_found,
        "recommendations_created": r.recommendations_created,
        "actions_executed": r.actions_executed,
        "error_message": r.error_message,
        "duration_ms": r.duration_ms,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


@router.post("/run", response_model=ScanResponse)
def run_scan(req: ScanBody = ScanBody(), db: Session = Depends(get_db)):
    """
    Run a full Observe → Reason → Plan → Act pipeline. Blocking — returns
    counts at the end. For long scans use /scan/run-async + /scan/status.
    """
    orch = Orchestrator(db)
    result = orch.run_scan(provider=req.provider, dry_run=req.dry_run)

    scanned = 0
    for sub in result.get("telemetry", {}).values():
        scanned += (sub or {}).get("scanned_resources", 0)

    return ScanResponse(
        provider=req.provider,
        scanned_resources=scanned,
        anomalies_found=result.get("anomalies", 0),
        recommendations_created=result.get("recommendations", 0),
        duration_ms=result.get("duration_ms", 0),
        details=result,
    )


@router.post("/run-async")
def run_scan_async(
    req: ScanBody = ScanBody(),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    """
    Trigger a background scan. Returns the scan_run_id IMMEDIATELY so the
    UI can poll /scan/status/{id} to render progress.
    """
    # Create the ScanRun row eagerly so the UI has an id to poll
    run = ScanRun(
        provider=req.provider,
        dry_run=req.dry_run,
        trigger="manual",
        status="running",
        current_step="queued",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    def _job():
        from app.database import SessionLocal
        s = SessionLocal()
        try:
            # Mark this orchestrator as continuing the existing row
            orch = Orchestrator(s)
            # Re-fetch the row in this new session and use it directly
            existing = s.query(ScanRun).filter(ScanRun.id == run_id).first()
            if existing:
                # Re-use the row instead of creating a new one
                orch._new_run = lambda *a, **k: existing
            orch.run_scan(
                provider=req.provider,
                dry_run=req.dry_run,
                trigger="manual",
            )
        except Exception as e:
            # Record failure
            existing = s.query(ScanRun).filter(ScanRun.id == run_id).first()
            if existing:
                existing.status = "failed"
                existing.error_message = str(e)[:1000]
                existing.finished_at = datetime.utcnow()
                s.commit()
        finally:
            s.close()

    background_tasks.add_task(_job)
    return {"scan_run_id": run_id, "status": "started"}


@router.get("/status/{run_id}")
def scan_status(run_id: int, db: Session = Depends(get_db)):
    """Poll the current progress of an async scan."""
    run = db.query(ScanRun).filter(ScanRun.id == run_id).first()
    if not run:
        raise HTTPException(404, "Scan run not found")
    return _run_dict(run)


@router.get("/status")
def latest_scan(db: Session = Depends(get_db)):
    """Most recent scan run (any status)."""
    run = db.query(ScanRun).order_by(ScanRun.id.desc()).first()
    if not run:
        return {"status": "never_run"}
    return _run_dict(run)


@router.get("/history")
def scan_history(limit: int = 20, db: Session = Depends(get_db)):
    """Recent scan history for the Reports / Settings page."""
    rows = (
        db.query(ScanRun)
        .order_by(ScanRun.id.desc())
        .limit(min(limit, 100))
        .all()
    )
    return [_run_dict(r) for r in rows]
