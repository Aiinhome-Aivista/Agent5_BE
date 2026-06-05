"""
AWS Service — real boto3 integration for the agent's Tool layer.

Capabilities:
- EC2 instances inventory & utilization (CloudWatch)
- RDS instances inventory & utilization
- S3 buckets size
- Cost Explorer (daily cost by service)
- Stop/Start/Terminate (Action Executor)

All operations honor: schema-driven inputs, idempotency, structured errors,
timeouts, retry with backoff, logging.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import settings

logger = logging.getLogger(__name__)

# Retry config: 3 attempts, exponential backoff
_RETRY = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((BotoCoreError, ClientError)),
    reraise=True,
)


class AWSService:
    """Wraps AWS SDK with the agent's tool conventions."""

    def __init__(
        self,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: Optional[str] = None,
    ):
        self.access_key = access_key or settings.AWS_ACCESS_KEY_ID
        self.secret_key = secret_key or settings.AWS_SECRET_ACCESS_KEY
        self.region = region or settings.AWS_DEFAULT_REGION
        self._cfg = Config(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=30,
        )

    # ---------- session ----------
    def _session(self, region: Optional[str] = None) -> boto3.Session:
        kwargs = {"region_name": region or self.region}
        if self.access_key and self.secret_key:
            kwargs["aws_access_key_id"] = self.access_key
            kwargs["aws_secret_access_key"] = self.secret_key
        return boto3.Session(**kwargs)

    def client(self, service: str, region: Optional[str] = None):
        return self._session(region).client(service, config=self._cfg)

    # ---------- account info ----------
    @_RETRY
    def get_account_id(self) -> str:
        if settings.AWS_ACCOUNT_ID:
            return settings.AWS_ACCOUNT_ID
        sts = self.client("sts")
        return sts.get_caller_identity()["Account"]

    # ---------- EC2 ----------
    @_RETRY
    def list_ec2_instances(self, region: str) -> List[Dict[str, Any]]:
        """Return EC2 instances in a region."""
        ec2 = self.client("ec2", region=region)
        results: List[Dict[str, Any]] = []
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    results.append({
                        "instance_id": inst["InstanceId"],
                        "instance_type": inst["InstanceType"],
                        "state": inst["State"]["Name"],
                        "launch_time": inst.get("LaunchTime").isoformat() if inst.get("LaunchTime") else None,
                        "az": inst.get("Placement", {}).get("AvailabilityZone"),
                        "region": region,
                        "name": tags.get("Name", inst["InstanceId"]),
                        "tags": tags,
                        "private_ip": inst.get("PrivateIpAddress"),
                        "vpc_id": inst.get("VpcId"),
                    })
        return results

    @_RETRY
    def get_ec2_cpu_utilization(
        self, instance_id: str, region: str, days: int = 14
    ) -> Dict[str, Any]:
        """Fetch avg + max CPU% over a window from CloudWatch."""
        cw = self.client("cloudwatch", region=region)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        resp = cw.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start,
            EndTime=end,
            Period=3600,
            Statistics=["Average", "Maximum"],
            Unit="Percent",
        )
        points = resp.get("Datapoints", [])
        if not points:
            return {"avg": None, "max": None, "samples": 0,
                    "window_start": start, "window_end": end}
        avg = sum(p["Average"] for p in points) / len(points)
        mx = max(p["Maximum"] for p in points)
        return {
            "avg": round(avg, 2),
            "max": round(mx, 2),
            "samples": len(points),
            "window_start": start,
            "window_end": end,
        }

    # ---------- RDS ----------
    @_RETRY
    def list_rds_instances(self, region: str) -> List[Dict[str, Any]]:
        rds = self.client("rds", region=region)
        results = []
        paginator = rds.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            for db in page.get("DBInstances", []):
                results.append({
                    "db_instance_id": db["DBInstanceIdentifier"],
                    "db_instance_class": db["DBInstanceClass"],
                    "engine": db["Engine"],
                    "status": db["DBInstanceStatus"],
                    "allocated_storage_gb": db["AllocatedStorage"],
                    "multi_az": db.get("MultiAZ", False),
                    "region": region,
                })
        return results

    @_RETRY
    def get_rds_cpu_utilization(
        self, db_instance_id: str, region: str, days: int = 14
    ) -> Dict[str, Any]:
        cw = self.client("cloudwatch", region=region)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        resp = cw.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_instance_id}],
            StartTime=start,
            EndTime=end,
            Period=3600,
            Statistics=["Average", "Maximum"],
            Unit="Percent",
        )
        points = resp.get("Datapoints", [])
        if not points:
            return {"avg": None, "max": None, "samples": 0,
                    "window_start": start, "window_end": end}
        return {
            "avg": round(sum(p["Average"] for p in points) / len(points), 2),
            "max": round(max(p["Maximum"] for p in points), 2),
            "samples": len(points),
            "window_start": start,
            "window_end": end,
        }

    # ---------- Cost Explorer ----------
    @_RETRY
    def get_daily_cost_by_service(self, days: int = 30) -> List[Dict[str, Any]]:
        """Returns list of {usage_date, service_name, cost, currency}."""
        # Cost Explorer endpoint lives in us-east-1
        ce = self.client("ce", region="us-east-1")
        end = datetime.utcnow().date()
        start = end - timedelta(days=days)
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        out: List[Dict[str, Any]] = []
        for period in resp.get("ResultsByTime", []):
            usage_date = datetime.fromisoformat(period["TimePeriod"]["Start"])
            for grp in period.get("Groups", []):
                metric = grp["Metrics"]["UnblendedCost"]
                amount = float(metric["Amount"])
                if amount <= 0:
                    continue
                currency = metric.get("Unit", "USD")
                out.append({
                    "usage_date": usage_date,
                    "service_name": grp["Keys"][0],
                    "cost": round(amount, 4),
                    "currency": currency,
                })
        return out

    # ---------- Action Executor: write operations ----------
    @_RETRY
    def stop_ec2_instance(
        self, instance_id: str, region: str, dry_run: bool = True
    ) -> Dict[str, Any]:
        ec2 = self.client("ec2", region=region)
        try:
            resp = ec2.stop_instances(InstanceIds=[instance_id], DryRun=dry_run)
            return {
                "success": True,
                "dry_run": dry_run,
                "previous_state": resp["StoppingInstances"][0]["PreviousState"]["Name"],
                "current_state": resp["StoppingInstances"][0]["CurrentState"]["Name"],
            }
        except ClientError as e:
            # DryRunOperation means dry-run validated successfully
            if e.response["Error"]["Code"] == "DryRunOperation":
                return {"success": True, "dry_run": True, "message": "Dry-run validated"}
            raise

    @_RETRY
    def start_ec2_instance(
        self, instance_id: str, region: str, dry_run: bool = False
    ) -> Dict[str, Any]:
        ec2 = self.client("ec2", region=region)
        try:
            resp = ec2.start_instances(InstanceIds=[instance_id], DryRun=dry_run)
            return {
                "success": True,
                "dry_run": dry_run,
                "previous_state": resp["StartingInstances"][0]["PreviousState"]["Name"],
                "current_state": resp["StartingInstances"][0]["CurrentState"]["Name"],
            }
        except ClientError as e:
            if e.response["Error"]["Code"] == "DryRunOperation":
                return {"success": True, "dry_run": True, "message": "Dry-run validated"}
            raise

    @_RETRY
    def modify_ec2_instance_type(
        self, instance_id: str, new_type: str, region: str, dry_run: bool = True
    ) -> Dict[str, Any]:
        """Right-size an EC2 instance (requires stop → modify → start)."""
        ec2 = self.client("ec2", region=region)
        if dry_run:
            return {"success": True, "dry_run": True,
                    "would_change_to": new_type}
        # In real prod: must be in 'stopped' state first
        resp = ec2.modify_instance_attribute(
            InstanceId=instance_id,
            InstanceType={"Value": new_type},
        )
        return {"success": True, "dry_run": False, "new_type": new_type,
                "request_id": resp["ResponseMetadata"]["RequestId"]}

    # ---------- helpers ----------
    def scan_regions(self) -> List[str]:
        return settings.aws_regions_list

    def is_configured(self) -> bool:
        """True if credentials are available (either explicit or via env/IAM role)."""
        return bool(self.access_key and self.secret_key) or bool(settings.AWS_ACCESS_KEY_ID)

    def test_connection(self) -> Dict[str, Any]:
        """
        Live connectivity check — calls STS GetCallerIdentity.
        Returns {ok, account_id, arn, error}.
        """
        try:
            sts = self.client("sts")
            ident = sts.get_caller_identity()
            return {
                "ok": True,
                "account_id": ident.get("Account"),
                "arn": ident.get("Arn"),
                "user_id": ident.get("UserId"),
                "region": self.region,
            }
        except (BotoCoreError, ClientError) as e:
            return {"ok": False, "error": str(e), "error_type": e.__class__.__name__}
        except Exception as e:
            return {"ok": False, "error": str(e), "error_type": e.__class__.__name__}


def aws_service_from_account(account) -> "AWSService":
    """
    Factory: build an AWSService from a CloudAccount DB row.
    Decrypts the stored credentials and uses the account's region.
    """
    from app.services.crypto import decrypt
    return AWSService(
        access_key=decrypt(account.aws_access_key_id_enc),
        secret_key=decrypt(account.aws_secret_access_key_enc),
        region=account.region or (settings.aws_regions_list[0] if settings.aws_regions_list else "us-east-1"),
    )
