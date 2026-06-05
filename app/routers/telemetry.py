"""Telemetry, cost, and anomaly read endpoints for the dashboard."""
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.telemetry import TelemetrySnapshot, CostSnapshot, AnomalyEvent
from app.models.recommendation import Recommendation
from app.schemas.schemas import TelemetryOut, CostOut, AnomalyOut

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])


@router.get("/snapshots", response_model=List[TelemetryOut])
def list_snapshots(
    provider: Optional[str] = None,
    account_id: Optional[str] = None,
    resource_type: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    q = db.query(TelemetrySnapshot)
    if provider:
        q = q.filter(TelemetrySnapshot.provider == provider)
    if account_id:
        q = q.filter(TelemetrySnapshot.account_id == account_id)
    if resource_type:
        q = q.filter(TelemetrySnapshot.resource_type == resource_type)
    return q.order_by(TelemetrySnapshot.collected_at.desc()).limit(limit).all()


@router.get("/cost", response_model=List[CostOut])
def list_costs(
    provider: Optional[str] = None,
    account_id: Optional[str] = None,
    days: int = 30,
    db: Session = Depends(get_db),
):
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = db.query(CostSnapshot).filter(CostSnapshot.usage_date >= cutoff)
    if provider:
        q = q.filter(CostSnapshot.provider == provider)
    if account_id:
        q = q.filter(CostSnapshot.account_id == account_id)
    return q.order_by(CostSnapshot.usage_date.desc()).limit(2000).all()


@router.get("/cost/summary")
def cost_summary(
    provider: Optional[str] = None,
    account_id: Optional[str] = None,
    days: int = 30,
    db: Session = Depends(get_db),
):
    """Aggregated cost summary by day + by service for charts. Currency-aware."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    q = db.query(CostSnapshot).filter(CostSnapshot.usage_date >= cutoff)
    if provider:
        q = q.filter(CostSnapshot.provider == provider)
    if account_id:
        q = q.filter(CostSnapshot.account_id == account_id)

    # Per-currency totals to detect mixed currencies
    by_currency = (
        q.with_entities(
            CostSnapshot.currency,
            func.sum(CostSnapshot.cost_usd).label("total")
        )
        .group_by(CostSnapshot.currency)
        .all()
    )
    currency_totals = {(r.currency or "USD"): float(r.total or 0) for r in by_currency}
    # Primary currency = the one with the biggest spend share
    primary_currency = (
        max(currency_totals, key=currency_totals.get) if currency_totals else "USD"
    )

    by_day = (
        q.with_entities(
            CostSnapshot.usage_date,
            func.sum(CostSnapshot.cost_usd).label("total")
        )
        .group_by(CostSnapshot.usage_date)
        .order_by(CostSnapshot.usage_date.asc())
        .all()
    )
    by_service = (
        q.with_entities(
            CostSnapshot.service_name,
            CostSnapshot.provider,
            func.sum(CostSnapshot.cost_usd).label("total")
        )
        .group_by(CostSnapshot.service_name, CostSnapshot.provider)
        .order_by(desc("total"))
        .limit(15)
        .all()
    )
    return {
        "total": float(sum(r.total for r in by_day) or 0),
        "currency": primary_currency,
        "currency_totals": currency_totals,
        "by_day": [
            {"date": r.usage_date.date().isoformat(), "cost": float(r.total)}
            for r in by_day
        ],
        "by_service": [
            {"service": r.service_name, "provider": r.provider, "cost": float(r.total)}
            for r in by_service
        ],
    }


@router.get("/anomalies", response_model=List[AnomalyOut])
def list_anomalies(
    provider: Optional[str] = None,
    account_id: Optional[str] = None,
    unresolved_only: bool = True,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    q = db.query(AnomalyEvent)
    if unresolved_only:
        q = q.filter(AnomalyEvent.resolved_at.is_(None))
    if provider:
        q = q.filter(AnomalyEvent.provider == provider)
    if account_id:
        q = q.filter(AnomalyEvent.account_id == account_id)
    return q.order_by(AnomalyEvent.detected_at.desc()).limit(limit).all()


# @router.get("/dashboard/overview")
# def dashboard_overview(
#     provider: Optional[str] = None,
#     db: Session = Depends(get_db),
# ):
#     """One-shot summary for the dashboard header tiles. Currency-aware."""
#     cutoff = datetime.utcnow() - timedelta(days=7)
#     qc = db.query(CostSnapshot).filter(CostSnapshot.usage_date >= cutoff)
#     if provider and provider != "all":
#         qc = qc.filter(CostSnapshot.provider == provider)
#     total_spend_7d = qc.with_entities(func.sum(CostSnapshot.cost_usd)).scalar() or 0

#     # Detect the primary currency for this scope
#     by_currency = (
#         qc.with_entities(
#             CostSnapshot.currency,
#             func.sum(CostSnapshot.cost_usd).label("total")
#         )
#         .group_by(CostSnapshot.currency)
#         .all()
#     )
#     currency_totals = {(r.currency or "USD"): float(r.total or 0) for r in by_currency}
#     primary_currency = (
#         max(currency_totals, key=currency_totals.get) if currency_totals else "USD"
#     )

#     qa = db.query(AnomalyEvent).filter(AnomalyEvent.resolved_at.is_(None))
#     if provider and provider != "all":
#         qa = qa.filter(AnomalyEvent.provider == provider)
#     open_anoms = qa.count()

#     qr = db.query(Recommendation).filter(Recommendation.status == "pending")
#     if provider and provider != "all":
#         qr = qr.filter(Recommendation.provider == provider)
#     pending_recs = qr.count()
#     pending_savings = qr.with_entities(
#         func.sum(Recommendation.estimated_monthly_savings_usd)
#     ).scalar() or 0

#     qt = db.query(TelemetrySnapshot).filter(
#         TelemetrySnapshot.collected_at >= cutoff
#     )
#     if provider and provider != "all":
#         qt = qt.filter(TelemetrySnapshot.provider == provider)
#     resources_monitored = qt.with_entities(
#         func.count(func.distinct(TelemetrySnapshot.resource_id))
#     ).scalar() or 0

#     return {
#         "total_spend_7d": float(total_spend_7d),
#         "currency": primary_currency,
#         "currency_totals": currency_totals,
#         "open_anomalies": int(open_anoms),
#         "pending_recommendations": int(pending_recs),
#         "potential_monthly_savings": float(pending_savings),
#         "resources_monitored": int(resources_monitored),
#     }

@router.get("/dashboard/overview")
def dashboard_overview(
    provider: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Dashboard overview with deduplicated cost calculations."""

    cutoff = datetime.utcnow() - timedelta(days=30)

    # ---------------------------------------------------
    # COST QUERY (remove duplicate snapshots)
    # ---------------------------------------------------

    cost_subquery = (
        db.query(
            CostSnapshot.service_name,
            CostSnapshot.provider,
            CostSnapshot.usage_date,
            func.max(CostSnapshot.id).label("latest_id")
        )
        .filter(
            CostSnapshot.usage_date >= cutoff
        )
    )

    if provider and provider != "all":
        cost_subquery = cost_subquery.filter(
            CostSnapshot.provider == provider
        )

    cost_subquery = (
        cost_subquery.group_by(
            CostSnapshot.service_name,
            CostSnapshot.provider,
            CostSnapshot.usage_date
        )
        .subquery()
    )

    qc = (
        db.query(CostSnapshot)
        .join(
            cost_subquery,
            CostSnapshot.id == cost_subquery.c.latest_id
        )
    )

    total_spend_7d = (
        qc.with_entities(
            func.sum(CostSnapshot.cost_usd)
        ).scalar()
        or 0
    )

    # -----------------------------------------
    # Currency summary
    # -----------------------------------------

    by_currency = (
        qc.with_entities(
            CostSnapshot.currency,
            func.sum(
                CostSnapshot.cost_usd
            ).label("total")
        )
        .group_by(
            CostSnapshot.currency
        )
        .all()
    )

    currency_totals = {
        (r.currency or "USD"): float(r.total or 0)
        for r in by_currency
    }

    primary_currency = (
        max(
            currency_totals,
            key=currency_totals.get
        )
        if currency_totals
        else "USD"
    )

    # -----------------------------------------
    # Open anomalies
    # -----------------------------------------

    qa = db.query(
        AnomalyEvent
    ).filter(
        AnomalyEvent.resolved_at.is_(None)
    )

    if provider and provider != "all":
        qa = qa.filter(
            AnomalyEvent.provider == provider
        )

    open_anoms = qa.count()

    # -----------------------------------------
    # Recommendations
    # -----------------------------------------

    qr = db.query(
        Recommendation
    ).filter(
        Recommendation.status == "pending"
    )

    if provider and provider != "all":
        qr = qr.filter(
            Recommendation.provider == provider
        )

    pending_recs = qr.count()

    pending_savings = (
        qr.with_entities(
            func.sum(
                Recommendation.estimated_monthly_savings_usd
            )
        ).scalar()
        or 0
    )

    # -----------------------------------------
    # Monitored resources
    # -----------------------------------------

    qt = db.query(
        TelemetrySnapshot
    ).filter(
        TelemetrySnapshot.collected_at >= cutoff
    )

    if provider and provider != "all":
        qt = qt.filter(
            TelemetrySnapshot.provider == provider
        )

    resources_monitored = (
        qt.with_entities(
            func.count(
                func.distinct(
                    TelemetrySnapshot.resource_id
                )
            )
        ).scalar()
        or 0
    )

    return {
        "total_spend_7d": round(
            float(total_spend_7d), 2
        ),

        "currency": primary_currency,

        "currency_totals": currency_totals,

        "open_anomalies": int(
            open_anoms
        ),

        "pending_recommendations": int(
            pending_recs
        ),

        "potential_monthly_savings": round(
            float(pending_savings), 2
        ),

        "resources_monitored": int(
            resources_monitored
        ),
    }
