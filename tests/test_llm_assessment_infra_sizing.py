"""
Tests for llm_assessment.infra_sizing -- deterministic Databricks
infra-sizing recommendations from database/table size metadata.
"""
from __future__ import annotations

from llm_assessment.infra_sizing import build_infra_sizing


def _table(schema, name, size_mb=0.0, row_count=0):
    return {"schema": schema, "name": name, "size_mb": size_mb, "row_count": row_count}


def test_empty_manifest_produces_no_recommendations():
    assert build_infra_sizing({}) == []


def test_tiny_estate_recommends_2x_small_warehouse():
    manifest = {"tables": [_table("dbo", "Orders", size_mb=100, row_count=10_000)], "databases": [{"size_mb": 272}]}
    recs = build_infra_sizing(manifest)
    warehouse = next(r for r in recs if r.category == "SQL_WAREHOUSE_SIZE")
    assert "2X-Small" in warehouse.recommendation


def test_large_estate_recommends_a_bigger_warehouse_tier():
    # 2 TB of table data -> should land in the "Large" tier (1TB-5TB bucket).
    manifest = {"tables": [_table("dbo", "Huge", size_mb=2 * 1024 * 1024, row_count=1)], "databases": []}
    recs = build_infra_sizing(manifest)
    warehouse = next(r for r in recs if r.category == "SQL_WAREHOUSE_SIZE")
    assert warehouse.recommendation.startswith("Large")


def test_database_size_used_when_larger_than_summed_table_sizes():
    # Index/log overhead means database size_mb can exceed the sum of table sizes.
    manifest = {"tables": [_table("dbo", "Orders", size_mb=10, row_count=100)], "databases": [{"size_mb": 50 * 1024}]}  # 50 GB
    recs = build_infra_sizing(manifest)
    warehouse = next(r for r in recs if r.category == "SQL_WAREHOUSE_SIZE")
    assert "50.00 GB" in warehouse.current_metric


def test_small_row_count_recommends_single_node_ingestion_cluster():
    manifest = {"tables": [_table("dbo", "T", size_mb=1, row_count=500)], "databases": []}
    recs = build_infra_sizing(manifest)
    ingestion = next(r for r in recs if r.category == "INGESTION_CLUSTER")
    assert "Single-node" in ingestion.recommendation


def test_large_row_count_recommends_bigger_ingestion_cluster():
    manifest = {"tables": [_table("dbo", "T", size_mb=1, row_count=500_000_000)], "databases": []}
    recs = build_infra_sizing(manifest)
    ingestion = next(r for r in recs if r.category == "INGESTION_CLUSTER")
    assert "Large autoscaling" in ingestion.recommendation


def test_no_table_over_1tb_recommends_no_partitioning():
    manifest = {"tables": [_table("dbo", "Orders", size_mb=30, row_count=10_000)], "databases": []}
    recs = build_infra_sizing(manifest)
    layout = next(r for r in recs if r.category == "TABLE_LAYOUT_STRATEGY")
    assert "No partitioning needed" in layout.recommendation
    assert "dbo.Orders" in layout.current_metric


def test_table_over_1tb_recommends_partitioning_for_that_table():
    manifest = {"tables": [
        _table("dbo", "Small", size_mb=10, row_count=100),
        _table("dbo", "Huge", size_mb=2 * 1024 * 1024, row_count=1_000_000_000),  # 2 TB
    ], "databases": []}
    recs = build_infra_sizing(manifest)
    layout = next(r for r in recs if r.category == "TABLE_LAYOUT_STRATEGY")
    assert "dbo.Huge" in layout.current_metric
    assert "Partition these specific tables" in layout.recommendation
    # The small table should NOT trigger partitioning advice for itself.
    assert "dbo.Small" not in layout.current_metric


def test_no_tables_produces_no_recommendations_at_all():
    assert build_infra_sizing({"tables": [], "databases": []}) == []
