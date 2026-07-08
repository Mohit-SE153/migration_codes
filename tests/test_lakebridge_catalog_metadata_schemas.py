"""
Tests for lakebridge_discovery.catalog_metadata.schemas -- pure object-
inventory discovery from sys.schemas only. Exercised against a stub
connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import schemas
from lakebridge_discovery.schema import LakebridgeDiscoveryResult


class _FakeCursor:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def execute(self, sql: str):
        return self

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


def test_discover_emits_schema_objects():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("dbo",), ("Sales",), ("HumanResources",)])

    schemas.discover(connection, result, seen_edges=set())

    names = {obj.name for obj in result.schemas}
    assert names == {"dbo", "Sales", "HumanResources"}
    assert all(obj.object_type == "schema" and obj.raw_category == "sys.schemas" for obj in result.schemas)


def test_discover_deduplicates_identical_rows():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("Sales",), ("Sales",)])

    schemas.discover(connection, result, seen_edges=set())

    assert len(result.schemas) == 1


def test_discover_does_not_touch_dependencies():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("dbo",)])

    schemas.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_schemas_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "schemas" in names
