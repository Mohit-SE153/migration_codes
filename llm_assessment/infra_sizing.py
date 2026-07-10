"""
Databricks infrastructure-sizing recommendations, derived from Discovery's
database/table size metadata (databases[].size_mb, tables[].size_mb/row_count).

Deterministic and threshold-based -- no LLM call, unlike complexity_scorer.py.
Sizing a warehouse/cluster is a lookup against known specs, not a judgment
call an LLM adds value to; keeping it deterministic also means it's free
and instant to re-run.

Grounded in Databricks' own published guidance (fetched directly from
their docs while building this, not from training-data memory, since
sizing tables drift over time):
  - SQL warehouse t-shirt sizes (2X-Small through 5X-Large) and their
    driver/worker instance types -- source: "SQL warehouse sizing, scaling,
    and queuing behavior", https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-behavior
    (AWS instance types shown; Azure/GCP use an equivalent node type at
    the same t-shirt tier -- check your workspace's available node types).
  - "Don't partition tables under 1TB; use Liquid Clustering instead" --
    source: "When to partition tables on Databricks",
    https://docs.databricks.com/aws/en/tables/partitions, and "Use liquid
    clustering for tables", https://docs.databricks.com/aws/en/tables/clustering
    (Liquid Clustering is Databricks' current default recommendation for
    all new Delta tables as of 2026).

The GB/TB thresholds mapping data VOLUME to a warehouse t-shirt size below
are this module's own assumption, not a Databricks-published rule -- real
SQL warehouse sizing depends on query concurrency and complexity, which
static Discovery metadata can't see at all. Treat every recommendation
here as a capacity-planning starting point to validate once you have real
query patterns, never a committed spec.
"""
from __future__ import annotations

from llm_assessment.schema import InfraSizingRecommendation

MB = 1.0
GB = 1024 * MB
TB = 1024 * GB

# (upper_bound_mb, warehouse_size, driver/worker instance type, worker count) --
# checked in order, first match wins. Instance types are the AWS reference
# from Databricks' own sizing table (see module docstring).
_WAREHOUSE_TIERS: tuple[tuple[float, str, str, int], ...] = (
    (10 * GB, "2X-Small", "i3.2xlarge", 1),
    (100 * GB, "X-Small", "i3.2xlarge", 2),
    (500 * GB, "Small", "i3.4xlarge", 4),
    (1 * TB, "Medium", "i3.8xlarge", 8),
    (5 * TB, "Large", "i3.8xlarge", 16),
    (20 * TB, "X-Large", "i3.16xlarge", 32),
    (50 * TB, "2X-Large", "i3.16xlarge", 64),
    (100 * TB, "3X-Large", "i3.16xlarge", 128),
    (float("inf"), "4X-Large (or larger -- consider workload isolation across multiple warehouses)", "i3.16xlarge", 256),
)

# (upper_bound_row_count, cluster description)
_INGESTION_CLUSTER_TIERS: tuple[tuple[float, str], ...] = (
    (1_000_000, "Single-node cluster (e.g. 1x i3.xlarge-equivalent) -- trivial data volume, no parallelism needed"),
    (10_000_000, "Small autoscaling job cluster (2-4 workers)"),
    (100_000_000, "Medium autoscaling job cluster (4-8 workers), consider parallelizing by schema/database"),
    (1_000_000_000, "Large autoscaling job cluster (8-32 workers), parallelize the migration by schema/database"),
    (float("inf"), "Multiple large autoscaling job clusters running in parallel across databases/schemas -- "
                    "at this scale, also evaluate a dedicated migration/ingestion tool (e.g. LakeFlow Connect) "
                    "over a single hand-rolled job"),
)

_PARTITION_THRESHOLD_MB = 1 * TB


def _format_size(size_mb: float) -> str:
    if size_mb >= TB:
        return f"{size_mb / TB:.2f} TB"
    if size_mb >= GB:
        return f"{size_mb / GB:.2f} GB"
    return f"{size_mb:.2f} MB"


def _warehouse_tier_for(total_size_mb: float) -> tuple[str, str, int]:
    for upper_bound, size_name, instance_type, worker_count in _WAREHOUSE_TIERS:
        if total_size_mb < upper_bound:
            return size_name, instance_type, worker_count
    return _WAREHOUSE_TIERS[-1][1:]


def _ingestion_cluster_for(total_rows: int) -> str:
    for upper_bound, description in _INGESTION_CLUSTER_TIERS:
        if total_rows < upper_bound:
            return description
    return _INGESTION_CLUSTER_TIERS[-1][1]


def build_infra_sizing(manifest: dict) -> list[InfraSizingRecommendation]:
    tables = manifest.get("tables", [])
    databases = manifest.get("databases", [])

    total_table_size_mb = sum(t.get("size_mb", 0) or 0 for t in tables)
    total_db_size_mb = sum(d.get("size_mb", 0) or 0 for d in databases)
    # Database-reported size (includes indexes/logs/free space) is usually
    # >= the sum of table data sizes -- use whichever is larger as the more
    # conservative sizing input.
    total_size_mb = max(total_table_size_mb, total_db_size_mb)
    total_rows = sum(t.get("row_count", 0) or 0 for t in tables)

    recommendations: list[InfraSizingRecommendation] = []

    if total_size_mb > 0 or tables:
        warehouse_size, instance_type, worker_count = _warehouse_tier_for(total_size_mb)
        recommendations.append(InfraSizingRecommendation(
            category="SQL_WAREHOUSE_SIZE",
            current_metric=f"Total estate size: {_format_size(total_size_mb)} across {len(tables)} table(s) in {len(databases)} database(s)",
            recommendation=f"{warehouse_size} SQL Warehouse ({worker_count}x {instance_type}-equivalent worker(s), AWS reference spec)",
            rationale="Databricks' own guidance is to start with a single larger warehouse and let serverless "
                      "autoscaling manage concurrency, sizing down if it proves oversized -- this figure is a "
                      "data-volume-only starting point for that conversation, not a substitute for it. Query "
                      "concurrency/complexity (unknown from static metadata) is usually the bigger sizing factor "
                      "in steady state.",
        ))

    if tables:
        recommendations.append(InfraSizingRecommendation(
            category="INGESTION_CLUSTER",
            current_metric=f"{total_rows:,} total row(s) across {len(tables)} table(s)",
            recommendation=_ingestion_cluster_for(total_rows),
            rationale="Sized for the one-time bulk migration/backfill load, not steady-state BI querying (that's "
                      "the SQL_WAREHOUSE_SIZE recommendation above). Actual throughput also depends on source "
                      "SQL Server I/O capacity and network bandwidth, neither of which Discovery metadata captures.",
        ))

    large_tables = sorted(
        (t for t in tables if (t.get("size_mb", 0) or 0) * MB >= _PARTITION_THRESHOLD_MB),
        key=lambda t: t.get("size_mb", 0), reverse=True,
    )
    if large_tables:
        names = ", ".join(f"{t['schema']}.{t['name']} ({_format_size(t['size_mb'])})" for t in large_tables[:5])
        recommendations.append(InfraSizingRecommendation(
            category="TABLE_LAYOUT_STRATEGY",
            current_metric=f"{len(large_tables)} table(s) at or above the 1TB partitioning threshold: {names}",
            recommendation="Partition these specific tables (Databricks' own threshold: only worth it above ~1TB); "
                           "use Liquid Clustering for every other table.",
            rationale="Source: Databricks 'When to partition tables' docs -- tables under 1TB shouldn't be "
                      "partitioned at all, and partitions should hold at least 1GB each when you do partition.",
        ))
    elif tables:
        largest = max(tables, key=lambda t: t.get("size_mb", 0) or 0)
        recommendations.append(InfraSizingRecommendation(
            category="TABLE_LAYOUT_STRATEGY",
            current_metric=f"Largest table: {largest['schema']}.{largest['name']} at {_format_size(largest.get('size_mb', 0))} "
                            f"({largest.get('row_count', 0):,} rows) -- well under the 1TB partitioning threshold",
            recommendation="No partitioning needed for any table in this estate. Use Liquid Clustering only if a "
                           "specific, already-known query pattern benefits from it; at these sizes it's unlikely "
                           "to be worth the operational overhead.",
            rationale="Source: Databricks 'When to partition tables' docs -- 'Databricks recommends you do not "
                      "partition tables that contain less than a terabyte of data.' Liquid Clustering is the "
                      "current (2026) default recommendation for new Delta tables generally, independent of size.",
        ))

    return recommendations
