"""FastAPI app entrypoint — Platform Performance & Cost Optimization Agent."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import sys

from app.config import settings
from app.database import init_db
from app.routers import (
    scan,
    telemetry,
    recommendations,
    chat,
    actions,
    reports,
    admin,
    accounts,
    login,
    upload,
    rules,
)

# Configure loguru
logger.remove()
logger.add(
    sys.stderr,
    level=settings.LOG_LEVEL,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    logger.info(" Starting {} ({})", settings.APP_NAME, settings.ENVIRONMENT)
    try:
        init_db()
        logger.info("MySQL tables initialized")
    except Exception as e:
        logger.error("Database init failed: {}", e)

    # Seed playbooks if not already done (non-fatal if it fails)
    try:
        from app.rag.ingest_playbooks import seed_playbooks
        seed_playbooks(force=False)
    except Exception as e:
        logger.warning("Playbook seeding skipped/failed: {}", e)

    # Auto-scan scheduler (driven by SCAN_INTERVAL_MINUTES in .env)
    try:
        from app.services.scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        logger.warning("Scheduler start skipped/failed: {}", e)

    yield

    logger.info("Shutting down {}", settings.APP_NAME)
    try:
        from app.services.scheduler import stop_scheduler
        stop_scheduler()
    except Exception:
        pass


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "PwC Agent 5 — Platform Performance & Cost Optimization Agent. "
        "Multi-agent AIOps/FinOps system that observes cloud telemetry & cost across AWS and Azure, "
        "reasons via Mistral LLM + RAG playbooks, plans and executes optimizations with guardrails, "
        "and answers operator questions over the entire data corpus."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(scan.router)
app.include_router(telemetry.router)
app.include_router(recommendations.router)
app.include_router(chat.router)
app.include_router(actions.router)
app.include_router(reports.router)
app.include_router(admin.router)
app.include_router(accounts.router)
app.include_router(login.router)
app.include_router(upload.router)
app.include_router(rules.router)


@app.get("/")
def root():
    return {
        "service": settings.APP_NAME,
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health():
    return {"status": "ok"}
