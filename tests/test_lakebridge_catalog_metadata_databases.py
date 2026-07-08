"""
Tests for lakebridge_discovery.catalog_metadata.databases -- pure
single-row object-inventory discovery from sys.databases/DATABASEPROPERTYEX
only. Exercised against a stub connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import databases
from lakebridge_discovery.schema import LakebridgeDiscoveryResult


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


def test_discover_emits_single_database_summary_row():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(("AdventureWorks2022", 278528.0, 71, 10, 20, "FULL", "160", "SQL_Latin1_General_CP1_CI_AS"))

    databases.discover(connection, result, seen_edges=set())

    assert len(result.databases) == 1
    db = result.databases[0]
    assert db.name == "AdventureWorks2022"
    assert db.size_mb == 278528.0
    assert db.table_count == 71
    assert db.proc_count == 10
    assert db.view_count == 20
    assert db.recovery_model == "FULL"
    assert db.compatibility_level == "160"
    assert db.collation_name == "SQL_Latin1_General_CP1_CI_AS"


def test_discover_handles_null_size_gracefully():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(("EmptyDb", None, 0, 0, 0, "SIMPLE", "160", None))

    databases.discover(connection, result, seen_edges=set())

    assert result.databases[0].size_mb == 0.0


def test_discover_no_row_is_a_noop():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(None)

    databases.discover(connection, result, seen_edges=set())

    assert result.databases == []


def test_discover_does_not_touch_dependencies():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(("Db", 1.0, 1, 1, 1, "FULL", "160", "Latin1"))

    databases.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_databases_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "databases" in names
