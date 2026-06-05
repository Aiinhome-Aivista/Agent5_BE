"""ScanRun — tracks every scan execution so the UI can show progress."""
from sqlalchemy import Column, Integer, String, DateTime, JSON, Text, Boolean
from sqlalchemy.sql import func
from app.database import Base


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(20), nullable=False)   # 'aws' | 'azure' | 'all'
    trigger = Column(String(20), nullable=False, default="manual")  # 'manual' | 'scheduled'
    dry_run = Column(Boolean, default=False)

    status = Column(String(20), nullable=False, default="running", index=True)
    # running | succeeded | failed
    current_step = Column(String(50), nullable=True)
    # collecting_telemetry | detecting_anomalies | analyzing | auto_executing | done

    scanned_resources = Column(Integer, default=0)
    anomalies_found = Column(Integer, default=0)
    recommendations_created = Column(Integer, default=0)
    actions_executed = Column(Integer, default=0)

    error_message = Column(Text, nullable=True)
    details = Column(JSON, nullable=True)

    started_at = Column(DateTime, server_default=func.now(), nullable=False)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)
