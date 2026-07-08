"""
Tests for lakebridge_discovery.catalog_metadata.clr_assemblies -- pure
object-inventory discovery from sys.assemblies/sys.database_principals
only. Exercised against a stub connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import clr_assemblies
from lakebridge_discovery.schema import LakebridgeDiscoveryResult


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql: str):
        return self

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


def test_discover_emits_assembly_with_schema_qualified_name():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("dbo", "MyClrAssembly", "SAFE", True)])

    clr_assemblies.discover(connection, result, seen_edges=set())

    assert len(result.assemblies) == 1
    obj = result.assemblies[0]
    assert obj.name == "dbo.MyClrAssembly"
    assert obj.object_type == "clr_assembly"
    assert "permission_set=SAFE" in obj.notes
    assert "is_visible=True" in obj.notes


def test_discover_falls_back_to_bare_name_when_no_owning_schema():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([(None, "OrphanAssembly", "UNSAFE", False)])

    clr_assemblies.discover(connection, result, seen_edges=set())

    assert result.assemblies[0].name == "OrphanAssembly"


def test_discover_deduplicates_identical_names():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([
        ("dbo", "MyClrAssembly", "SAFE", True),
        ("dbo", "MyClrAssembly", "SAFE", True),
    ])

    clr_assemblies.discover(connection, result, seen_edges=set())

    assert len(result.assemblies) == 1


def test_discover_no_rows_is_a_noop():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([])

    clr_assemblies.discover(connection, result, seen_edges=set())

    assert result.assemblies == []


def test_discover_does_not_touch_dependencies():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("dbo", "A", "SAFE", True)])

    clr_assemblies.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_clr_assemblies_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "clr_assemblies" in names
