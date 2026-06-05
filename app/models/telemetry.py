"""Telemetry snapshots, cost snapshots, and anomaly events."""
from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ForeignKey, Index
from sqlalchemy.sql import func
from app.database import Base


class TelemetrySnapshot(Base):
    """A point-in-time utilization snapshot for a resource."""
    __tablename__ = "telemetry_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(20), nullable=False, index=True)
    account_id = Column(String(255), nullable=False, index=True)
    region = Column(String(100), nullable=True)
    resource_type = Column(String(100), nullable=False)  # ec2, rds, vm, sql_db, etc.
    resource_id = Column(String(500), nullable=False)
    resource_name = Column(String(500), nullable=True)
    cpu_avg_pct = Column(Float, nullable=True)
    cpu_max_pct = Column(Float, nullable=True)
    memory_avg_pct = Column(Float, nullable=True)
    network_in_bytes = Column(Float, nullable=True)
    network_out_bytes = Column(Float, nullable=True)
    extra_metrics = Column(JSON, nullable=True)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    collected_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_tel_resource_window", "resource_id", "window_end"),
    )


class CostSnapshot(Base):
    """Daily cost broken down by service."""
    __tablename__ = "cost_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(20), nullable=False, index=True)
    account_id = Column(String(255), nullable=False, index=True)
    service_name = Column(String(255), nullable=False)
    cost_usd = Column(Float, nullable=False)
    currency = Column(String(10), default="USD", nullable=False)
    usage_date = Column(DateTime, nullable=False, index=True)
    extra = Column(JSON, nullable=True)
    collected_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_cost_account_date", "account_id", "usage_date"),
    )


class AnomalyEvent(Base):
    """Detected anomalies — cost spikes, idle resources, perf regressions."""
    __tablename__ = "anomaly_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(20), nullable=False)
    account_id = Column(String(255), nullable=False, index=True)
    anomaly_type = Column(String(100), nullable=False)  # cost_spike, idle_resource, latency_regression
    severity = Column(String(20), nullable=False)  # low, medium, high, critical
    resource_id = Column(String(500), nullable=True)
    resource_type = Column(String(100), nullable=True)
    title = Column(String(500), nullable=False)
    description = Column(String(2000), nullable=True)
    metric_value = Column(Float, nullable=True)
    baseline_value = Column(Float, nullable=True)
    deviation_pct = Column(Float, nullable=True)
    details = Column(JSON, nullable=True)
    detected_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    resolved_at = Column(DateTime, nullable=True)
