"""
Tests for lakebridge_discovery.catalog_metadata.indexes -- pure object-
inventory discovery from sys.indexes/sys.tables/sys.schemas only. Exercised
against a stub connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import indexes
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


def test_discover_emits_index_object():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("Sales", "SalesOrderHeader", "PK_SalesOrderHeader_SalesOrderID", "CLUSTERED")])

    indexes.discover(connection, result, seen_edges=set())

    assert len(result.indexes) == 1
    obj = result.indexes[0]
    assert obj.object_type == "index"
    assert obj.name == "Sales.SalesOrderHeader.PK_SalesOrderHeader_SalesOrderID"
    assert obj.source_tech == "MS SQL Server"
    assert obj.raw_category == "sys.indexes"
    assert obj.notes == "type=CLUSTERED"


def test_discover_disambiguates_same_named_index_on_different_tables():
    """Index names are only unique per-table, not per-schema -- two
    different tables can each have an index literally named
    "IX_Something", and both must be retained as distinct objects."""
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([
        ("Sales", "SalesOrderHeader", "IX_Something", "NONCLUSTERED"),
        ("Sales", "SalesOrderDetail", "IX_Something", "NONCLUSTERED"),
    ])

    indexes.discover(connection, result, seen_edges=set())

    names = {obj.name for obj in result.indexes}
    assert names == {
        "Sales.SalesOrderHeader.IX_Something",
        "Sales.SalesOrderDetail.IX_Something",
    }


def test_discover_deduplicates_identical_rows():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([
        ("Sales", "SalesOrderHeader", "PK_SalesOrderHeader_SalesOrderID", "CLUSTERED"),
        ("Sales", "SalesOrderHeader", "PK_SalesOrderHeader_SalesOrderID", "CLUSTERED"),
    ])

    indexes.discover(connection, result, seen_edges=set())

    assert len(result.indexes) == 1


def test_discover_does_not_touch_dependencies():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("Sales", "SalesOrderHeader", "PK_X", "CLUSTERED")])

    indexes.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_indexes_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "indexes" in names


def test_query_excludes_hypothetical_indexes():
    """Regression guard: hypothetical (DTA what-if) indexes aren't real
    deployed objects and shouldn't inflate the index inventory -- see
    autovista.sql_metadata_extractor.QUERY_INDEXES's comment for the full
    index-category analysis this defensive filter is drawn from."""
    from lakebridge_discovery.catalog_metadata.indexes import _QUERY_INDEXES
    assert "is_hypothetical" in _QUERY_INDEXES
