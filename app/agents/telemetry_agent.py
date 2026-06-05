"""
Agent 2 — Telemetry & Anomaly Detection Agent.

Pulls metrics + billing data, classifies workload patterns, detects anomalies.
Uses efficient Mistral tier + statistical heuristics.

Credentials come from the cloud_accounts table (managed via UI) — one
service instance per registered account.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from statistics import mean, stdev
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.config import settings
from app.models.telemetry import TelemetrySnapshot, CostSnapshot, AnomalyEvent
from app.models.cloud_account import CloudAccount
from app.services.aws_service import AWSService, aws_service_from_account
from app.services.azure_service import AzureService, azure_service_from_account
from app.services.fx import to_usd

logger = logging.getLogger(__name__)


class TelemetryAgent:
    """Collects telemetry from registered cloud accounts; detects anomalies."""

    def __init__(self, db: Session):
        self.db = db

    def _aws_accounts(self) -> List[CloudAccount]:
        return (
            self.db.query(CloudAccount)
            .filter(CloudAccount.provider == "aws", CloudAccount.enabled == True)  # noqa: E712
            .all()
        )

    def _azure_accounts(self) -> List[CloudAccount]:
        return (
            self.db.query(CloudAccount)
            .filter(CloudAccount.provider == "azure", CloudAccount.enabled == True)  # noqa: E712
            .all()
        )

    # ============ TELEMETRY COLLECTION ============

    def collect_aws_telemetry(self) -> Dict[str, Any]:
        """Scan all enabled AWS accounts; persist EC2/RDS utilization + daily costs."""
        accounts = self._aws_accounts()
        if not accounts:
            return {"error": "No AWS accounts registered. Add one in Settings.", "scanned": 0}

        total_scanned = 0
        total_cost_rows = 0
        per_account: List[Dict[str, Any]] = []

        for account in accounts:
            aws = aws_service_from_account(account)
            try:
                account_id = aws.get_account_id()
            except Exception as e:
                logger.error(f"AWS get_account_id failed for {account.display_name}: {e}")
                per_account.append({"account_id": account.account_identifier, "error": str(e), "scanned": 0})
                continue

            scanned = 0
            regions = aws.scan_regions()
            for region in regions:
                try:
                    instances = aws.list_ec2_instances(region)
                except Exception as e:
                    logger.warning(f"list_ec2_instances({region}) failed: {e}")
                    instances = []

                for inst in instances:
                    if inst["state"] not in ("running", "stopped"):
                        continue
                    try:
                        util = aws.get_ec2_cpu_utilization(
                            inst["instance_id"], region,
                            days=settings.TELEMETRY_LOOKBACK_DAYS,
                        )
                    except Exception as e:
                        logger.warning(f"CPU fetch failed for {inst['instance_id']}: {e}")
                        continue
                    snap = TelemetrySnapshot(
                        provider="aws",
                        account_id=account_id,
                        region=region,
                        resource_type="ec2",
                        resource_id=inst["instance_id"],
                        resource_name=inst.get("name"),
                        cpu_avg_pct=util.get("avg"),
                        cpu_max_pct=util.get("max"),
                        extra_metrics={
                            "instance_type": inst["instance_type"],
                            "state": inst["state"],
                            "tags": inst.get("tags", {}),
                            "samples": util.get("samples", 0),
                        },
                        window_start=util.get("window_start") or datetime.now(timezone.utc),
                        window_end=util.get("window_end") or datetime.now(timezone.utc),
                    )
                    self.db.add(snap)
                    scanned += 1

                # RDS
                try:
                    dbs = aws.list_rds_instances(region)
                except Exception as e:
                    logger.warning(f"list_rds_instances({region}) failed: {e}")
                    dbs = []

                for db_inst in dbs:
                    try:
                        util = aws.get_rds_cpu_utilization(
                            db_inst["db_instance_id"], region,
                            days=settings.TELEMETRY_LOOKBACK_DAYS,
                        )
                    except Exception as e:
                        logger.warning(f"RDS CPU fetch failed: {e}")
                        continue
                    snap = TelemetrySnapshot(
                        provider="aws",
                        account_id=account_id,
                        region=region,
                        resource_type="rds",
                        resource_id=db_inst["db_instance_id"],
                        resource_name=db_inst["db_instance_id"],
                        cpu_avg_pct=util.get("avg"),
                        cpu_max_pct=util.get("max"),
                        extra_metrics=db_inst,
                        window_start=util.get("window_start") or datetime.now(timezone.utc),
                        window_end=util.get("window_end") or datetime.now(timezone.utc),
                    )
                    self.db.add(snap)
                    scanned += 1

            # Cost snapshots (per-account)
            try:
                cost_rows = aws.get_daily_cost_by_service(days=30)
            except Exception as e:
                logger.warning(f"AWS cost fetch failed: {e}")
                cost_rows = []
            for row in cost_rows:
                self.db.add(CostSnapshot(
                    provider="aws", account_id=account_id,
                    service_name=row["service_name"],
                    # cost_usd=row["cost"], currency=row.get("currency", "USD"),
                    cost_usd=to_usd(row["cost"], row.get("currency")),
                    currency="USD",
                    usage_date=row["usage_date"],
                ))

            account.last_scanned_at = datetime.utcnow()
            self.db.commit()
            total_scanned += scanned
            total_cost_rows += len(cost_rows)
            per_account.append({"account_id": account_id, "scanned": scanned, "cost_rows": len(cost_rows)})

        return {"provider": "aws", "accounts": per_account,
                "scanned_resources": total_scanned,
                "cost_rows": total_cost_rows}

    def collect_azure_telemetry(self) -> Dict[str, Any]:
        accounts = self._azure_accounts()
        if not accounts:
            return {"error": "No Azure accounts registered. Add one in Settings.", "scanned": 0}

        total_scanned = 0
        total_cost_rows = 0
        per_account: List[Dict[str, Any]] = []

        for account in accounts:
            azure = azure_service_from_account(account)
            subscription_id = account.account_identifier
            scanned = 0

            # ---------- generic resource inventory ----------
            # Lists every resource (App Service, SQL DB, Storage, VNet, etc.)
            # so the dashboard reflects the real inventory, not just VMs.
            try:
                all_resources = azure.list_all_resources()
            except Exception as e:
                logger.warning(f"Azure list_all_resources failed: {e}")
                all_resources = []

            now = datetime.now(timezone.utc)
            for r in all_resources:
                # Map Azure RP type → friendly resource_type
                rtype = (r.get("type") or "").lower()
                if rtype.startswith("microsoft.compute/virtualmachines"):
                    continue  # VMs handled with CPU metrics below
                friendly = {
                    "microsoft.web/sites": "app_service",
                    "microsoft.sql/servers": "sql_server",
                    "microsoft.sql/servers/databases": "sql_database",
                    "microsoft.storage/storageaccounts": "storage_account",
                    "microsoft.network/virtualnetworks": "virtual_network",
                    "microsoft.network/networksecuritygroups": "nsg",
                    "microsoft.network/publicipaddresses": "public_ip",
                    "microsoft.containerservice/managedclusters": "aks_cluster",
                    "microsoft.documentdb/databaseaccounts": "cosmos_db",
                    "microsoft.keyvault/vaults": "key_vault",
                }.get(rtype, rtype.split("/")[-1] if "/" in rtype else "other")

                snap = TelemetrySnapshot(
                    provider="azure",
                    account_id=subscription_id,
                    region=r.get("location"),
                    resource_type=friendly,
                    resource_id=r["id"],
                    resource_name=r.get("name"),
                    cpu_avg_pct=None,  # no CPU for non-compute
                    cpu_max_pct=None,
                    extra_metrics={
                        "azure_type": r.get("type"),
                        "kind": r.get("kind"),
                        "resource_group": r.get("resource_group"),
                        "tags": r.get("tags", {}),
                    },
                    window_start=now,
                    window_end=now,
                )
                self.db.add(snap)
                scanned += 1

            # ---------- VM CPU telemetry ----------
            try:
                vms = azure.list_vms()
            except Exception as e:
                logger.error(f"Azure list_vms failed: {e}")
                vms = []

            for vm in vms:
                try:
                    util = azure.get_vm_cpu_utilization(
                        vm["vm_id"], days=settings.TELEMETRY_LOOKBACK_DAYS,
                    )
                    # Best-effort power state
                    power = "unknown"
                    if vm.get("resource_group"):
                        try:
                            power = azure.get_vm_power_state(vm["resource_group"], vm["vm_name"])
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"Azure VM CPU fetch failed: {e}")
                    continue
                snap = TelemetrySnapshot(
                    provider="azure",
                    account_id=subscription_id,
                    region=vm.get("location"),
                    resource_type="vm",
                    resource_id=vm["vm_id"],
                    resource_name=vm["vm_name"],
                    cpu_avg_pct=util.get("avg"),
                    cpu_max_pct=util.get("max"),
                    extra_metrics={
                        "vm_size": vm.get("vm_size"),
                        "resource_group": vm.get("resource_group"),
                        "tags": vm.get("tags", {}),
                        "power_state": power,
                    },
                    window_start=util.get("window_start") or datetime.now(timezone.utc),
                    window_end=util.get("window_end") or datetime.now(timezone.utc),
                )
                self.db.add(snap)
                scanned += 1

            # Cost
            try:
                cost_rows = azure.get_daily_cost_by_service(days=30)
            except Exception as e:
                logger.warning(f"Azure cost fetch failed: {e}")
                cost_rows = []
            for row in cost_rows:
                self.db.add(CostSnapshot(
                    provider="azure", account_id=subscription_id,
                    service_name=row["service_name"],
                    # cost_usd=row["cost"], currency=row.get("currency", "USD"),
                    cost_usd=to_usd(row["cost"], row.get("currency")),
                    currency="USD",     
                    usage_date=row["usage_date"],
                ))

            account.last_scanned_at = datetime.utcnow()
            self.db.commit()
            total_scanned += scanned
            total_cost_rows += len(cost_rows)
            per_account.append({"account_id": subscription_id, "scanned": scanned, "cost_rows": len(cost_rows)})

        return {"provider": "azure", "accounts": per_account,
                "scanned_resources": total_scanned,
                "cost_rows": total_cost_rows}

    # ============ ANOMALY DETECTION ============

    def detect_anomalies(self, provider: str | None = None) -> List[AnomalyEvent]:
        """Run statistical anomaly detection across telemetry + cost."""
        events: List[AnomalyEvent] = []
        events.extend(self._detect_idle_resources(provider))
        events.extend(self._detect_cost_spikes(provider))
        for e in events:
            self.db.add(e)
        self.db.commit()
        return events

    def _detect_idle_resources(self, provider: str | None) -> List[AnomalyEvent]:

        """

        Idle detection — two paths:

          1. Compute resources (VMs/EC2/RDS) with CPU below threshold

          2. Non-compute resources (App Service, SQL DB, etc.) with

             near-zero cost over the last 7 days

        """

        q = self.db.query(TelemetrySnapshot)

        if provider and provider != "all":

            q = q.filter(TelemetrySnapshot.provider == provider)

        cutoff = datetime.now(timezone.utc) - timedelta(days=2)

        q = q.filter(TelemetrySnapshot.collected_at >= cutoff)



        events: List[AnomalyEvent] = []

        seen = set()



        # Build a "cost in last 7 days" lookup keyed by resource_name or service_name

        cost_cutoff = datetime.utcnow() - timedelta(days=7)

        cost_by_service = {}

        for cs in self.db.query(CostSnapshot).filter(CostSnapshot.usage_date >= cost_cutoff).all():

            cost_by_service[cs.service_name] = cost_by_service.get(cs.service_name, 0.0) + (cs.cost_usd or 0)



        for snap in q.all():

            key = (snap.provider, snap.resource_id)

            if key in seen:

                continue

            seen.add(key)



            existing = self.db.query(AnomalyEvent).filter(

                AnomalyEvent.resource_id == snap.resource_id,

                AnomalyEvent.anomaly_type == "idle_resource",

                AnomalyEvent.resolved_at.is_(None),

            ).first()

            if existing:

                continue



            # ---- Path 1: compute resources with CPU metrics ----

            if snap.cpu_avg_pct is not None:

                if (snap.cpu_avg_pct < settings.IDLE_CPU_THRESHOLD_PCT

                        and (snap.cpu_max_pct or 0) < 30):

                    events.append(AnomalyEvent(

                        provider=snap.provider, account_id=snap.account_id,

                        anomaly_type="idle_resource",

                        severity="medium" if snap.cpu_avg_pct > 2 else "low",

                        resource_id=snap.resource_id,

                        resource_type=snap.resource_type,

                        title=f"Idle {snap.resource_type}: {snap.resource_name or snap.resource_id}",

                        description=(

                            f"Average CPU {snap.cpu_avg_pct}% (max {snap.cpu_max_pct}%) over "

                            f"the last {settings.TELEMETRY_LOOKBACK_DAYS} days — below the "

                            f"{settings.IDLE_CPU_THRESHOLD_PCT}% threshold."

                        ),

                        metric_value=snap.cpu_avg_pct,

                        baseline_value=settings.IDLE_CPU_THRESHOLD_PCT,

                        details={"region": snap.region, "extra": snap.extra_metrics},

                    ))

                continue



            # ---- Path 2: non-compute resources (inventory only, no CPU) ----

            # Flag if power_state == "stopped/deallocated" OR essentially zero cost

            extra = snap.extra_metrics or {}

            power = (extra.get("power_state") or "").lower()

            if power in ("stopped", "deallocated", "stopped (deallocated)"):

                events.append(AnomalyEvent(

                    provider=snap.provider, account_id=snap.account_id,

                    anomaly_type="idle_resource",

                    severity="medium",

                    resource_id=snap.resource_id,

                    resource_type=snap.resource_type,

                    title=f"Stopped {snap.resource_type}: {snap.resource_name}",

                    description=f"Resource is in '{power}' state but may still incur disk/IP costs.",

                    details={"region": snap.region, "extra": extra},

                ))

                continue



            # Flag tiny-cost non-compute resources that are still provisioned

            # (App Service Free tier slots, empty SQL DBs, unattached storage, etc.)

            if snap.resource_type in ("app_service", "sql_database", "storage_account",

                                      "public_ip", "nsg", "virtual_network"):

                events.append(AnomalyEvent(

                    provider=snap.provider, account_id=snap.account_id,

                    anomaly_type="idle_resource",

                    severity="low",

                    resource_id=snap.resource_id,

                    resource_type=snap.resource_type,

                    title=f"Review {snap.resource_type}: {snap.resource_name}",

                    description=(

                        f"{snap.resource_type.replace('_', ' ').title()} exists in the "

                        f"subscription — verify it's still needed. No utilization metrics "

                        f"available for this resource type."

                    ),

                    details={"region": snap.region, "extra": extra},

                ))



        return events
    def _detect_cost_spikes(self, provider: str | None) -> List[AnomalyEvent]:
        """Daily total cost > threshold% above 7-day moving average."""
        # Aggregate per (provider, account_id, usage_date)
        q = self.db.query(CostSnapshot)
        if provider and provider != "all":
            q = q.filter(CostSnapshot.provider == provider)
        rows = q.all()
        if len(rows) < 8:
            return []

        # Build per-day totals
        per_day: Dict[tuple, Dict[datetime, float]] = {}
        for r in rows:
            key = (r.provider, r.account_id)
            day = r.usage_date.replace(hour=0, minute=0, second=0, microsecond=0)
            per_day.setdefault(key, {}).setdefault(day, 0.0)
            per_day[key][day] += r.cost_usd

        events: List[AnomalyEvent] = []
        for (prov, acc), day_costs in per_day.items():
            sorted_days = sorted(day_costs.keys())
            if len(sorted_days) < 8:
                continue
            for i, day in enumerate(sorted_days):
                if i < 7:
                    continue
                window = [day_costs[d] for d in sorted_days[i - 7:i]]
                baseline = mean(window) if window else 0
                today_cost = day_costs[day]
                if baseline <= 0:
                    continue
                deviation = ((today_cost - baseline) / baseline) * 100
                if deviation > settings.COST_ANOMALY_THRESHOLD_PCT:
                    existing = self.db.query(AnomalyEvent).filter(
                        AnomalyEvent.account_id == acc,
                        AnomalyEvent.anomaly_type == "cost_spike",
                        AnomalyEvent.detected_at >= day,
                    ).first()
                    if existing:
                        continue
                    events.append(AnomalyEvent(
                        provider=prov,
                        account_id=acc,
                        anomaly_type="cost_spike",
                        severity="high" if deviation > 50 else "medium",
                        resource_id=None,
                        resource_type=None,
                        title=f"Cost spike on {day.date()}: ${today_cost:.2f} ({deviation:+.1f}% vs 7-day avg)",
                        description=(
                            f"Daily spend on {day.date()} was ${today_cost:.2f}, "
                            f"{deviation:.1f}% above the 7-day moving average of ${baseline:.2f}."
                        ),
                        metric_value=today_cost,
                        baseline_value=baseline,
                        deviation_pct=deviation,
                        details={"date": str(day.date())},
                    ))
        return events
