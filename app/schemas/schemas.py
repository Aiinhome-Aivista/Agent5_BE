"""Pydantic schemas for API request/response bodies."""
from datetime import datetime
from typing import Any, Optional, List, Dict
from pydantic import BaseModel, Field, ConfigDict


# --------- Login & Authentication ---------

class CaptchaResponse(BaseModel):
    captcha_id: str
    challenge: str


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    captcha_token: str = Field(..., description="Format: captcha_id:answer")


class LoginResponse(BaseModel):
    user_id: int
    email: str
    message: str = "Login successful"


# --------- Cloud Accounts ---------

class CloudAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    account_identifier: str
    display_name: str
    region_or_location: Optional[str] = None
    is_active: bool
    last_scanned_at: Optional[datetime] = None


# --------- Telemetry ---------

class TelemetryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    account_id: str
    region: Optional[str]
    resource_type: str
    resource_id: str
    resource_name: Optional[str]
    cpu_avg_pct: Optional[float]
    cpu_max_pct: Optional[float]
    memory_avg_pct: Optional[float]
    window_start: datetime
    window_end: datetime


class CostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    account_id: str
    service_name: str
    cost_usd: float
    usage_date: datetime


class AnomalyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    account_id: str
    anomaly_type: str
    severity: str
    resource_id: Optional[str]
    title: str
    description: Optional[str]
    metric_value: Optional[float]
    baseline_value: Optional[float]
    deviation_pct: Optional[float]
    detected_at: datetime
    resolved_at: Optional[datetime]


# --------- Recommendations ---------

class RecommendationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    account_id: str
    resource_id: Optional[str]
    resource_type: Optional[str]
    resource_name: Optional[str]
    title: str
    description: Optional[str]
    action_type: str
    risk_class: str
    estimated_monthly_savings_usd: float
    estimated_latency_improvement_pct: float
    confidence_score: float
    rollback_plan: Optional[str]
    rationale: Optional[str]
    scale_suggestion: Optional[str] = None
    status: str
    created_at: datetime
    approved_at: Optional[datetime]
    executed_at: Optional[datetime]


class RecommendationDecision(BaseModel):
    decision: str = Field(..., description="approve | reject")
    user_email: Optional[str] = None
    reason: Optional[str] = None


# --------- Action History ---------

class ActionHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    recommendation_id: Optional[int]
    provider: str
    account_id: str
    resource_id: Optional[str]
    action_type: str
    status: str
    actor: str
    error_message: Optional[str]
    realized_savings_usd: Optional[float]
    started_at: datetime
    completed_at: Optional[datetime]


# --------- Chat ---------

class ChatRequest(BaseModel):
    message: str
    session_uuid: Optional[str] = None
    user_email: Optional[str] = None
    # Optional scope filter
    provider: Optional[str] = None
    account_id: Optional[str] = None


class Citation(BaseModel):
    source: str
    snippet: str
    type: str  # "telemetry" | "cost" | "playbook" | "memory" | "recommendation"


class ChatResponse(BaseModel):
    session_uuid: str
    message: str
    summary: Optional[str] = None
    citations: List[Citation] = []
    latency_ms: int


# --------- Scans ---------

class ScanRequest(BaseModel):
    provider: str  # 'aws' | 'azure' | 'all'
    dry_run: bool = True


class ScanResponse(BaseModel):
    provider: str
    scanned_resources: int
    anomalies_found: int
    recommendations_created: int
    duration_ms: int
    details: Dict[str, Any] = {}


# --------- Generic ---------

class StatusResponse(BaseModel):
    status: str
    message: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
