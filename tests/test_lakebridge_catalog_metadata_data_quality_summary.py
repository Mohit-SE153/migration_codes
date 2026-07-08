"""
Tests for lakebridge_discovery.catalog_metadata.data_quality_summary --
metadata-driven migration-readiness indicators computed directly from
sys.tables/sys.columns/sys.types and friends (independent reimplementation
of autovista's build_data_quality_summary). Exercised against a stub
connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import data_quality_summary
from lakebridge_discovery.schema import DatabaseEntity, LakebridgeDiscoveryResult, LakebridgeObjectRef


class _FakeCursor:
    def __init__(self, table_rows, column_rows):
        self._table_rows = table_rows
        self._column_rows = column_rows
        self._rows: list[tuple] = []

    def execute(self, sql: str):
        self._rows = self._column_rows if "sys.columns" in sql else self._table_rows
        return self

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, table_rows=(), column_rows=()):
        self._table_rows = table_rows
        self._column_rows = column_rows

    def cursor(self):
        return _FakeCursor(self._table_rows, self._column_rows)


def test_discover_computes_table_level_indicators():
    result = LakebridgeDiscoveryResult()
    result.databases = [DatabaseEntity(name="AdventureWorks2022", size_mb=1.0, table_count=2, proc_count=0, view_count=0)]
    connection = _FakeConnection(table_rows=[
        # schema, table, row_count, is_heap, has_pk, has_fk, has_trigger, has_identity, has_computed, has_cdc, has_change_tracking, is_temporal
        ("dbo", "Orders", 100, 0, 1, 1, 0, 1, 0, 0, 0, 0),
        ("dbo", "StagingTable", 0, 1, 0, 0, 0, 0, 0, 0, 0, 0),
    ])

    data_quality_summary.discover(connection, result, seen_edges=set())

    assert len(result.data_quality_summary) == 1
    summary = result.data_quality_summary[0]
    assert summary.database == "AdventureWorks2022"
    assert summary.total_tables == 2
    assert summary.empty_tables == 1
    assert summary.tables_without_primary_key == 1
    assert summary.tables_without_foreign_key == 1
    assert summary.heap_tables == 1
    assert summary.tables_with_identity_columns == 1
    assert summary.largest_tables == ["dbo.Orders", "dbo.StagingTable"]


def test_discover_computes_column_level_indicators():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(column_rows=[
        ("varchar", True, -1),
        ("int", False, 4),
        ("text", True, 16),
        ("sql_variant", True, -1),
    ])

    data_quality_summary.discover(connection, result, seen_edges=set())

    summary = result.data_quality_summary[0]
    assert summary.nullable_columns == 3
    assert summary.non_nullable_columns == 1
    assert summary.large_max_columns == 1  # only the varchar(max) column
    assert summary.deprecated_data_type_columns == 1  # text
    assert summary.text_ntext_image_columns == 1
    assert summary.sql_variant_columns == 1


def test_discover_computes_excessive_index_tables_from_existing_result_indexes():
    result = LakebridgeDiscoveryResult()
    result.indexes = [
        LakebridgeObjectRef(object_type="index", name=f"dbo.WideTable.IX_{i}", source_tech="MS SQL Server")
        for i in range(11)
    ]
    connection = _FakeConnection()

    data_quality_summary.discover(connection, result, seen_edges=set())

    assert result.data_quality_summary[0].excessive_index_tables == ["dbo.WideTable"]


def test_discover_zero_tables_produces_zeroed_summary():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection()

    data_quality_summary.discover(connection, result, seen_edges=set())

    summary = result.data_quality_summary[0]
    assert summary.total_tables == 0
    assert summary.empty_tables == 0
    assert summary.largest_tables == []


def test_discover_does_not_touch_dependencies():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection()

    data_quality_summary.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_data_quality_summary_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "data_quality_summary" in names
