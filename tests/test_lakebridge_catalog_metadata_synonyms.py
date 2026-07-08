"""
Tests for lakebridge_discovery.catalog_metadata.synonyms -- pure object-
inventory discovery from sys.synonyms/sys.schemas only. Exercised against a
stub connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import synonyms
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


def test_discover_emits_synonym_object():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("dbo", "CustomerAlias", "dbo.Customers")])

    synonyms.discover(connection, result, seen_edges=set())

    assert len(result.synonyms) == 1
    obj = result.synonyms[0]
    assert obj.object_type == "synonym"
    assert obj.name == "dbo.CustomerAlias"
    assert obj.raw_category == "sys.synonyms"
    assert obj.notes == "base_object=dbo.Customers"


def test_discover_deduplicates_identical_rows():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([
        ("dbo", "CustomerAlias", "dbo.Customers"),
        ("dbo", "CustomerAlias", "dbo.Customers"),
    ])

    synonyms.discover(connection, result, seen_edges=set())

    assert len(result.synonyms) == 1


def test_discover_returns_empty_when_none_exist():
    """Matches this task's live-database finding: AdventureWorks2022 has
    zero synonyms -- the probe must correctly report an empty list, not
    fabricate placeholder objects."""
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([])

    synonyms.discover(connection, result, seen_edges=set())

    assert result.synonyms == []


def test_discover_does_not_touch_dependencies():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("dbo", "CustomerAlias", "dbo.Customers")])

    synonyms.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_synonyms_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "synonyms" in names
