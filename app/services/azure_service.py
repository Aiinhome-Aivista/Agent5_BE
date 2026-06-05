"""
Azure Service — real azure-sdk integration.

Capabilities:
- VM inventory & utilization (Azure Monitor)
- SQL DB inventory
- Storage accounts
- Cost Management daily breakdown
- Start/Stop/Deallocate VMs
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from azure.identity import ClientSecretCredential, DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
# from azure.mgmt.resource import ResourceManagementClient, SubscriptionClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.subscription import SubscriptionClient
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import (
    QueryDefinition,
    QueryDataset,
    QueryAggregation,
    QueryGrouping,
    QueryTimePeriod,
)
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.sql import SqlManagementClient
from azure.core.exceptions import HttpResponseError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import settings

logger = logging.getLogger(__name__)

_RETRY = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type(HttpResponseError),
    reraise=True,
)


class AzureService:
    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        subscription_id: Optional[str] = None,
    ):
        self.tenant_id = tenant_id or settings.AZURE_TENANT_ID
        self.client_id = client_id or settings.AZURE_CLIENT_ID
        self.client_secret = client_secret or settings.AZURE_CLIENT_SECRET
        self.subscription_id = subscription_id or settings.AZURE_SUBSCRIPTION_ID

        if self.tenant_id and self.client_id and self.client_secret:
            self.credential = ClientSecretCredential(
                tenant_id=self.tenant_id,
                client_id=self.client_id,
                client_secret=self.client_secret,
            )
        else:
            # Falls back to env / managed identity / az CLI
            self.credential = DefaultAzureCredential()

    # ---------- clients ----------
    def _compute(self) -> ComputeManagementClient:
        return ComputeManagementClient(self.credential, self.subscription_id)

    def _monitor(self) -> MonitorManagementClient:
        return MonitorManagementClient(self.credential, self.subscription_id)

    def _resource(self) -> ResourceManagementClient:
        return ResourceManagementClient(self.credential, self.subscription_id)

    def _cost(self) -> CostManagementClient:
        return CostManagementClient(self.credential)

    def _storage(self) -> StorageManagementClient:
        return StorageManagementClient(self.credential, self.subscription_id)

    def _sql(self) -> SqlManagementClient:
        return SqlManagementClient(self.credential, self.subscription_id)

    # ---------- inventory ----------
    @_RETRY
    def list_subscriptions(self) -> List[Dict[str, Any]]:
        sub_client = SubscriptionClient(self.credential)
        return [
            {"id": s.subscription_id, "display_name": s.display_name, "state": s.state}
            for s in sub_client.subscriptions.list()
        ]

    @_RETRY
    def list_vms(self) -> List[Dict[str, Any]]:
        cc = self._compute()
        results: List[Dict[str, Any]] = []
        for vm in cc.virtual_machines.list_all():
            results.append({
                "vm_id": vm.id,
                "vm_name": vm.name,
                "vm_size": vm.hardware_profile.vm_size if vm.hardware_profile else None,
                "location": vm.location,
                "os_type": (vm.storage_profile.os_disk.os_type.value
                            if vm.storage_profile and vm.storage_profile.os_disk else None),
                "resource_group": self._rg_from_id(vm.id),
                "tags": vm.tags or {},
                "provisioning_state": vm.provisioning_state,
            })
        return results

    @_RETRY
    def get_vm_power_state(self, resource_group: str, vm_name: str) -> str:
        cc = self._compute()
        view = cc.virtual_machines.instance_view(resource_group, vm_name)
        for s in (view.statuses or []):
            if s.code and s.code.startswith("PowerState/"):
                return s.code.split("/")[-1]  # running / deallocated / stopped
        return "unknown"

    @_RETRY
    def get_vm_cpu_utilization(self, vm_resource_id: str, days: int = 14) -> Dict[str, Any]:
        mc = self._monitor()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        timespan = f"{start.isoformat()}/{end.isoformat()}"
        resp = mc.metrics.list(
            resource_uri=vm_resource_id,
            timespan=timespan,
            interval="PT1H",
            metricnames="Percentage CPU",
            aggregation="Average,Maximum",
        )
        values = []
        max_values = []
        for metric in resp.value:
            for ts in metric.timeseries:
                for d in ts.data:
                    if d.average is not None:
                        values.append(d.average)
                    if d.maximum is not None:
                        max_values.append(d.maximum)
        if not values:
            return {"avg": None, "max": None, "samples": 0,
                    "window_start": start, "window_end": end}
        return {
            "avg": round(sum(values) / len(values), 2),
            "max": round(max(max_values) if max_values else max(values), 2),
            "samples": len(values),
            "window_start": start,
            "window_end": end,
        }

    @_RETRY
    def list_storage_accounts(self) -> List[Dict[str, Any]]:
        sc = self._storage()
        return [
            {
                "id": s.id,
                "name": s.name,
                "location": s.location,
                "sku": s.sku.name if s.sku else None,
                "kind": s.kind,
                "resource_group": self._rg_from_id(s.id),
            }
            for s in sc.storage_accounts.list()
        ]

    @_RETRY
    def list_sql_servers(self) -> List[Dict[str, Any]]:
        sql = self._sql()
        return [
            {
                "id": s.id,
                "name": s.name,
                "location": s.location,
                "version": s.version,
                "resource_group": self._rg_from_id(s.id),
            }
            for s in sql.servers.list()
        ]

    @_RETRY
    def list_all_resources(self) -> List[Dict[str, Any]]:
        """
        Inventory every resource in the subscription via the generic Resource API.
        Used so the dashboard counts all resources (App Services, SQL DBs,
        Storage, VNets, etc.), not just VMs with CPU metrics.
        """
        rc = self._resource()
        out: List[Dict[str, Any]] = []
        try:
            for r in rc.resources.list():
                out.append({
                    "id": r.id,
                    "name": r.name,
                    "type": r.type,                     # e.g. Microsoft.Web/sites
                    "kind": getattr(r, "kind", None),
                    "location": r.location,
                    "resource_group": self._rg_from_id(r.id),
                    "tags": dict(r.tags) if r.tags else {},
                })
        except Exception as e:
            logger.warning(f"list_all_resources failed: {e}")
        return out

    # ---------- cost ----------
    @_RETRY
    def get_daily_cost_by_service(self, days: int = 30) -> List[Dict[str, Any]]:
        """Returns list of {usage_date, service_name, cost, currency}."""
        cc = self._cost()
        scope = f"/subscriptions/{self.subscription_id}"
        end = datetime.utcnow()
        start = end - timedelta(days=days)

        query = QueryDefinition(
            type="ActualCost",
            timeframe="Custom",
            time_period=QueryTimePeriod(from_property=start, to=end),
            dataset=QueryDataset(
                granularity="Daily",
                aggregation={
                    "totalCost": QueryAggregation(name="Cost", function="Sum"),
                },
                grouping=[QueryGrouping(type="Dimension", name="ServiceName")],
            ),
        )
        resp = cc.query.usage(scope=scope, parameters=query)
        out: List[Dict[str, Any]] = []
        if not resp.rows:
            return out

        # Determine column indices
        cols = [c.name for c in resp.columns]
        try:
            cost_idx = cols.index("Cost")
        except ValueError:
            cost_idx = 0
        try:
            date_idx = cols.index("UsageDate")
        except ValueError:
            date_idx = 1
        try:
            svc_idx = cols.index("ServiceName")
        except ValueError:
            svc_idx = 2
        # Azure Cost Management returns Currency in its own column
        try:
            cur_idx = cols.index("Currency")
        except ValueError:
            cur_idx = None

        for row in resp.rows:
            try:
                cost = float(row[cost_idx])
                if cost <= 0:
                    continue
                # UsageDate often int yyyymmdd
                raw_date = row[date_idx]
                if isinstance(raw_date, int):
                    s = str(raw_date)
                    usage_date = datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]))
                else:
                    usage_date = datetime.fromisoformat(str(raw_date))
                service = str(row[svc_idx])
                currency = str(row[cur_idx]) if cur_idx is not None else "USD"
                out.append({
                    "usage_date": usage_date,
                    "service_name": service,
                    "cost": round(cost, 4),
                    "currency": currency,
                })
            except Exception as e:
                logger.warning(f"Skipping malformed cost row: {e}")
        return out

    # ---------- action executor ----------
    @_RETRY
    def deallocate_vm(self, resource_group: str, vm_name: str, dry_run: bool = True) -> Dict[str, Any]:
        if dry_run:
            return {"success": True, "dry_run": True,
                    "would_deallocate": f"{resource_group}/{vm_name}"}
        cc = self._compute()
        poller = cc.virtual_machines.begin_deallocate(resource_group, vm_name)
        return {"success": True, "dry_run": False, "status": poller.status()}

    @_RETRY
    def start_vm(self, resource_group: str, vm_name: str, dry_run: bool = False) -> Dict[str, Any]:
        if dry_run:
            return {"success": True, "dry_run": True,
                    "would_start": f"{resource_group}/{vm_name}"}
        cc = self._compute()
        poller = cc.virtual_machines.begin_start(resource_group, vm_name)
        return {"success": True, "dry_run": False, "status": poller.status()}

    @_RETRY
    def resize_vm(self, resource_group: str, vm_name: str, new_size: str,
                  dry_run: bool = True) -> Dict[str, Any]:
        if dry_run:
            return {"success": True, "dry_run": True, "would_resize_to": new_size}
        cc = self._compute()
        vm = cc.virtual_machines.get(resource_group, vm_name)
        vm.hardware_profile.vm_size = new_size
        poller = cc.virtual_machines.begin_create_or_update(resource_group, vm_name, vm)
        return {"success": True, "dry_run": False, "status": poller.status(),
                "new_size": new_size}

    # ---------- helpers ----------
    @staticmethod
    def _rg_from_id(resource_id: Optional[str]) -> Optional[str]:
        if not resource_id:
            return None
        parts = resource_id.split("/")
        try:
            i = parts.index("resourceGroups")
            return parts[i + 1]
        except (ValueError, IndexError):
            return None

    def is_configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret and self.subscription_id)

    def test_connection(self) -> Dict[str, Any]:
        """
        Live connectivity check — fetches subscription metadata.
        Returns {ok, subscription_id, display_name, tenant_id, error}.
        """
        try:
            sub_client = SubscriptionClient(self.credential)
            sub = sub_client.subscriptions.get(self.subscription_id)
            return {
                "ok": True,
                "subscription_id": sub.subscription_id,
                "display_name": sub.display_name,
                "tenant_id": self.tenant_id,
                "state": str(sub.state),
            }
        except HttpResponseError as e:
            return {"ok": False, "error": e.message or str(e), "error_type": "HttpResponseError",
                    "status_code": e.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e), "error_type": e.__class__.__name__}

    def list_subscriptions(self) -> List[Dict[str, Any]]:
        """List all subscriptions visible to the credential."""
        sub_client = SubscriptionClient(self.credential)
        return [
            {"subscription_id": s.subscription_id, "display_name": s.display_name, "state": str(s.state)}
            for s in sub_client.subscriptions.list()
        ]


def azure_service_from_account(account) -> "AzureService":
    """Factory: build AzureService from a CloudAccount DB row."""
    from app.services.crypto import decrypt
    return AzureService(
        tenant_id=decrypt(account.azure_tenant_id_enc),
        client_id=decrypt(account.azure_client_id_enc),
        client_secret=decrypt(account.azure_client_secret_enc),
        subscription_id=account.account_identifier,
    )
