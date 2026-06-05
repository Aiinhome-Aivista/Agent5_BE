"""
Seed playbooks into ChromaDB.

Run on first startup OR via the /admin/seed-playbooks endpoint.
These are the canonical optimization playbooks for AWS / Azure analytics platforms.
"""
import logging
from typing import List, Dict, Any

from app.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)


PLAYBOOKS: List[Dict[str, Any]] = [
    {
        "title": "AWS EC2 right-sizing for steady workloads",
        "provider": "aws",
        "resource_type": "ec2",
        "category": "right_sizing",
        "content": (
            "When an EC2 instance has avg CPU below 10% and max CPU below 25% over a 14-day window, "
            "it is a strong candidate for right-sizing. Recommended approach: "
            "(1) Identify the instance family (e.g. m5, c5, t3). "
            "(2) Drop one size class (e.g. m5.xlarge -> m5.large). "
            "(3) Use modify_ec2_instance_type — instance must be in 'stopped' state first. "
            "(4) Measure for 24h post-change. Roll back if CPU sustained > 70%. "
            "Expected savings: 40-50% of compute cost for that resource. "
            "Risk: medium for prod workloads, low for dev/test. Always dry-run first."
        ),
    },
    {
        "title": "AWS EC2 idle instance elimination",
        "provider": "aws",
        "resource_type": "ec2",
        "category": "idle_resource",
        "content": (
            "EC2 instances with avg CPU < 2% AND network I/O near zero for 7+ days are typically "
            "idle dev/test resources. For non-prod tagged instances, the agent can auto-stop them "
            "(Low-risk action). For untagged or prod instances, generate a Medium-risk recommendation "
            "with 15-min approval window. Always preserve EBS volumes; stopping (not terminating) "
            "allows quick restart. Rollback: start_ec2_instance."
        ),
    },
    {
        "title": "AWS RDS right-sizing",
        "provider": "aws",
        "resource_type": "rds",
        "category": "right_sizing",
        "content": (
            "RDS DB instance with avg CPU < 15%, peak < 40%, and connections < 30% of max for 14 days "
            "is over-provisioned. Recommend dropping one tier (e.g. db.m5.xlarge -> db.m5.large). "
            "This is High-risk for prod (downtime ~5-10 min unless Multi-AZ). Always require human "
            "approval. Consider Aurora Serverless v2 for spiky workloads instead — it auto-scales ACUs."
        ),
    },
    {
        "title": "AWS S3 storage tiering",
        "provider": "aws",
        "resource_type": "s3",
        "category": "storage_tier",
        "content": (
            "S3 objects not accessed in 30+ days should move to S3 Intelligent-Tiering or Standard-IA; "
            "objects untouched 90+ days move to Glacier Instant Retrieval; 180+ days to Glacier Deep Archive. "
            "Use S3 Lifecycle policies — no per-object work. Savings: 40-95% depending on tier. "
            "Risk: low. Caveat: minimum 30-day billing for IA, 90-day for Glacier."
        ),
    },
    {
        "title": "Azure VM right-sizing",
        "provider": "azure",
        "resource_type": "vm",
        "category": "right_sizing",
        "content": (
            "Azure VM with avg CPU < 10% and max CPU < 30% over 14 days is over-provisioned. "
            "Use the Azure Advisor sizing recommendation if available, OR drop one tier within the "
            "same VM family (e.g. Standard_D4s_v5 -> Standard_D2s_v5). Resize requires stop-deallocate "
            "-> resize -> start. Brief downtime (~2 min). Medium risk for prod. "
            "Savings: 40-50% on that VM."
        ),
    },
    {
        "title": "Azure VM auto-shutdown for dev/test",
        "provider": "azure",
        "resource_type": "vm",
        "category": "idle_resource",
        "content": (
            "Azure dev/test VMs should be deallocated (not stopped) outside business hours. "
            "Deallocated VMs incur only storage costs (~5-10% of running cost). "
            "Use Azure Auto-shutdown feature OR the agent's deallocate_vm tool on a schedule. "
            "Tag-based policy recommended: 'env=dev' or 'env=test' VMs auto-deallocate at 8 PM local, "
            "auto-start at 8 AM. Savings: ~65% of compute cost. Risk: low."
        ),
    },
    {
        "title": "Azure SQL Database tier optimization",
        "provider": "azure",
        "resource_type": "sql_db",
        "category": "right_sizing",
        "content": (
            "Azure SQL DB on Provisioned tier with DTU/vCore utilization < 20% sustained should "
            "either drop a tier or migrate to Serverless tier (auto-pauses, billed per second). "
            "Serverless ideal for intermittent workloads with idle periods. "
            "Provisioned Hyperscale for high-throughput. Risk: medium — connection pool resets on tier change."
        ),
    },
    {
        "title": "Cost spike root cause analysis",
        "provider": "any",
        "resource_type": "any",
        "category": "anomaly_rca",
        "content": (
            "When daily cost spikes > 25% vs 7-day moving average, root-cause investigation steps: "
            "(1) Compare per-service breakdown vs baseline — isolate the spiking service. "
            "(2) For compute: check for new instances launched, instance count change, or autoscaling events. "
            "(3) For data egress: check NAT gateway / data transfer cost; often a misconfigured replication. "
            "(4) For warehouses (Snowflake/BigQuery): check query history for runaway queries — Cartesian joins, "
            "full table scans, missing WHERE clauses. "
            "(5) Generate recommendation with explicit citation of the metric/event that explains the spike."
        ),
    },
    {
        "title": "Snowflake warehouse auto-suspend tuning",
        "provider": "any",
        "resource_type": "warehouse",
        "category": "autosuspend",
        "content": (
            "Snowflake warehouses default to 10-min auto-suspend; for ETL or interactive analytics with "
            "long gaps between queries, drop to 60s auto-suspend. Use ALTER WAREHOUSE ... SET AUTO_SUSPEND = 60. "
            "Caveat: aggressive auto-suspend can cause cold-cache misses; balance with workload pattern. "
            "Recommended for non-production and bursty workloads. Savings: 15-30% of warehouse cost. Risk: low."
        ),
    },
    {
        "title": "Reserved Instances vs Savings Plans decision",
        "provider": "aws",
        "resource_type": "compute",
        "category": "commitment_discount",
        "content": (
            "If steady-state compute usage > 70% over 3 months, commit to Savings Plans (Compute Savings Plan "
            "preferred — covers EC2, Fargate, Lambda). 1-year SP = ~27% discount, 3-year = ~52%. "
            "RIs are less flexible — only consider for highly stable single-instance-family usage. "
            "Analyze using AWS Cost Explorer 'Savings Plans recommendation' report. Risk: medium (commitment risk)."
        ),
    },
    {
        "title": "Anomaly detection: idle resource heuristic",
        "provider": "any",
        "resource_type": "any",
        "category": "anomaly_detection",
        "content": (
            "Heuristic for idle resource detection: CPU avg < 5% AND CPU max < 15% AND network I/O < 1 MB/hr "
            "sustained for 7+ days. For storage: no read/write ops for 30+ days. "
            "For databases: connection count = 0 for 7+ days. "
            "Always cross-check with tags — production resources may legitimately be 'warm standby' "
            "(do not auto-stop). Surface as recommendation with explicit pre-conditions."
        ),
    },
    {
        "title": "Rollback procedure for failed optimization",
        "provider": "any",
        "resource_type": "any",
        "category": "rollback",
        "content": (
            "Every optimization action must register a rollback token before executing. Rollback procedure: "
            "(1) For instance type change: restore original instance type via modify_ec2_instance_type or "
            "Azure resize_vm. (2) For stop: start the instance again. (3) For schedule shift: restore prior cron. "
            "(4) For autosuspend change: ALTER WAREHOUSE ... SET AUTO_SUSPEND = <original>. "
            "Rollback must be idempotent and complete within 5 min for L/M-risk, 15 min for H-risk."
        ),
    },
]


def seed_playbooks(force: bool = False) -> Dict[str, Any]:
    vs = get_vector_store()
    existing = vs.playbooks.count()
    if existing > 0 and not force:
        return {"status": "skipped", "reason": "already_seeded", "count": existing}

    chunks = [pb["content"] for pb in PLAYBOOKS]
    metadatas = [
        {
            "title": pb["title"],
            "provider": pb["provider"],
            "resource_type": pb["resource_type"],
            "category": pb["category"],
        }
        for pb in PLAYBOOKS
    ]
    ids = vs.add_playbook_chunks(chunks, metadatas)
    logger.info(f"Seeded {len(ids)} playbooks")
    return {"status": "seeded", "count": len(ids)}
