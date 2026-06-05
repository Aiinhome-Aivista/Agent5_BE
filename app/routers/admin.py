"""Admin router — health checks, playbook seeding, vector store diagnostics."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from loguru import logger

from app.database import get_db
from app.config import settings
from app.rag.ingest_playbooks import seed_playbooks
from app.services.vector_store import get_vector_store
from app.services.mistral_service import mistral_service
from app.models.cloud_account import CloudAccount

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/health")
def health(db: Session = Depends(get_db)):
    """
    Health check for application subsystems:
      - MySQL connectivity
      - ChromaDB connectivity
      - Mistral API configuration
      - Registered cloud accounts (count & last-test status)
    """
    status = {"app": "ok"}

    # MySQL
    try:
        db.execute(text("SELECT 1"))
        status["mysql"] = "ok"
    except Exception as e:
        status["mysql"] = f"error: {str(e)[:120]}"

    # ChromaDB
    try:
        vs = get_vector_store()
        status["chromadb"] = vs.counts()
    except Exception as e:
        status["chromadb"] = f"error: {str(e)[:120]}"

    # Mistral
    status["mistral_configured"] = bool(settings.MISTRAL_API_KEY)
    status["mistral_frontier_model"] = settings.MISTRAL_FRONTIER_MODEL

    # Cloud accounts (per-account credentials managed via UI)
    accounts = db.query(CloudAccount).all()
    aws_accounts = [a for a in accounts if a.provider == "aws"]
    azure_accounts = [a for a in accounts if a.provider == "azure"]
    status["aws_accounts"] = {
        "total": len(aws_accounts),
        "enabled": sum(1 for a in aws_accounts if a.enabled),
        "ok": sum(1 for a in aws_accounts if a.last_test_status == "ok"),
        "error": sum(1 for a in aws_accounts if a.last_test_status == "error"),
    }
    status["azure_accounts"] = {
        "total": len(azure_accounts),
        "enabled": sum(1 for a in azure_accounts if a.enabled),
        "ok": sum(1 for a in azure_accounts if a.last_test_status == "ok"),
        "error": sum(1 for a in azure_accounts if a.last_test_status == "error"),
    }

    return status


@router.post("/seed-playbooks")
def seed_playbooks_endpoint(force: bool = False):
    """Seed the optimization playbooks knowledge base into ChromaDB."""
    try:
        result = seed_playbooks(force=force)
        return result
    except Exception as e:
        logger.exception("Seed playbooks failed: {}", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/vector-counts")
def vector_counts():
    """Return current document counts per ChromaDB collection."""
    vs = get_vector_store()
    return vs.counts()


@router.get("/settings")
def get_settings():
    """Return non-secret runtime settings for UI display."""
    return {
        "app_name": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "aws_regions": settings.aws_regions_list,
        "auto_execute_low_risk": settings.AUTO_EXECUTE_LOW_RISK,
        "blast_radius_per_hour": settings.BLAST_RADIUS_MAX_RESOURCES_PER_HOUR,
        "cost_anomaly_threshold_pct": settings.COST_ANOMALY_THRESHOLD_PCT,
        "idle_cpu_threshold_pct": settings.IDLE_CPU_THRESHOLD_PCT,
        "telemetry_lookback_days": settings.TELEMETRY_LOOKBACK_DAYS,
        "mistral_frontier_model": settings.MISTRAL_FRONTIER_MODEL,
        "mistral_efficient_model": settings.MISTRAL_EFFICIENT_MODEL,
        "scan_interval_minutes": settings.SCAN_INTERVAL_MINUTES,
        "scan_provider": settings.SCAN_PROVIDER,
        "scan_on_startup": settings.SCAN_ON_STARTUP,
    }


@router.get("/scheduler")
def scheduler_status_endpoint():
    """Auto-scan scheduler status (interval, next run)."""
    from app.services.scheduler import scheduler_status
    return scheduler_status()
