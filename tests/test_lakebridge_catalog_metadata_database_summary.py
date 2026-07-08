"""
Tests for lakebridge_discovery.catalog_metadata.database_summary -- an
aggregation probe that reads counts from result's own already-populated
inventory categories (tables/views/.../database_users/database_roles) plus
one direct catalog query for largest_table. Exercised against a stub
connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import database_summary
from lakebridge_discovery.schema import (
    DatabaseEntity,
    LakebridgeDiscoveryResult,
    LakebridgeObjectRef,
    ServerPrincipalEntity,
)


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def execute(self, sql: str):
        return self

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCursor(self._row)


def _populated_result() -> LakebridgeDiscoveryResult:
    result = LakebridgeDiscoveryResult()
    result.databases = [DatabaseEntity(
        name="AdventureWorks2022", size_mb=1000.0, table_count=2, proc_count=1, view_count=1,
        recovery_model="FULL", compatibility_level="160",
    )]
    result.tables = [LakebridgeObjectRef(object_type="table", name="dbo.A", source_tech="MS SQL Server")]
    result.views = [LakebridgeObjectRef(object_type="view", name="dbo.V", source_tech="MS SQL Server")]
    result.stored_procedures = [LakebridgeObjectRef(object_type="stored_procedure", name="dbo.P", source_tech="MS SQL Server")]
    result.schemas = [LakebridgeObjectRef(object_type="schema", name="dbo", source_tech="MS SQL Server")]
    result.indexes = [LakebridgeObjectRef(object_type="index", name="dbo.A.PK_A", source_tech="MS SQL Server")]
    result.constraints = [
        LakebridgeObjectRef(object_type="constraint", name="dbo.PK_A", source_tech="MS SQL Server", notes="PK"),
        LakebridgeObjectRef(object_type="constraint", name="dbo.FK_A", source_tech="MS SQL Server", notes="FOREIGN_KEY_CONSTRAINT"),
    ]
    result.synonyms = [LakebridgeObjectRef(object_type="synonym", name="dbo.S", source_tech="MS SQL Server")]
    result.sequences = [LakebridgeObjectRef(object_type="sequence", name="dbo.Seq", source_tech="MS SQL Server")]
    result.database_users = [ServerPrincipalEntity(name="AppUser", principal_type="USER")]
    result.database_roles = [ServerPrincipalEntity(name="db_datareader", principal_type="ROLE")]
    return result


def test_discover_aggregates_counts_from_already_populated_result():
    result = _populated_result()
    connection = _FakeConnection(("dbo.A",))

    database_summary.discover(connection, result, seen_edges=set())

    assert len(result.database_summary) == 1
    summary = result.database_summary[0]
    assert summary.database == "AdventureWorks2022"
    assert summary.total_tables == 1
    assert summary.total_views == 1
    assert summary.total_stored_procedures == 1
    assert summary.total_schemas == 1
    assert summary.total_indexes == 1
    assert summary.total_constraints == 2
    assert summary.total_foreign_keys == 1
    assert summary.total_synonyms == 1
    assert summary.total_sequences == 1
    assert summary.total_users == 1
    assert summary.total_roles == 1
    assert summary.database_size_mb == 1000.0
    assert summary.recovery_model == "FULL"
    assert summary.compatibility_level == "160"
    assert summary.largest_table == "dbo.A"


def test_discover_handles_no_largest_table_row():
    result = _populated_result()
    connection = _FakeConnection(None)

    database_summary.discover(connection, result, seen_edges=set())

    assert result.database_summary[0].largest_table is None


def test_discover_degrades_gracefully_when_databases_probe_did_not_run():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(None)

    database_summary.discover(connection, result, seen_edges=set())

    summary = result.database_summary[0]
    assert summary.database == ""
    assert summary.database_size_mb == 0.0
    assert summary.total_tables == 0


def test_discover_does_not_touch_dependencies():
    result = _populated_result()
    connection = _FakeConnection(None)

    database_summary.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_database_summary_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "database_summary" in names
