"""
Tests for lakebridge_discovery.catalog_metadata.sequences -- pure object-
inventory discovery from sys.sequences/sys.schemas only. Exercised against
a stub connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import sequences
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


def test_discover_emits_sequence_object():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("dbo", "OrderNumberSequence")])

    sequences.discover(connection, result, seen_edges=set())

    assert len(result.sequences) == 1
    obj = result.sequences[0]
    assert obj.object_type == "sequence"
    assert obj.name == "dbo.OrderNumberSequence"
    assert obj.source_tech == "MS SQL Server"
    assert obj.raw_category == "sys.sequences"


def test_discover_deduplicates_identical_rows():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([
        ("dbo", "OrderNumberSequence"),
        ("dbo", "OrderNumberSequence"),
    ])

    sequences.discover(connection, result, seen_edges=set())

    assert len(result.sequences) == 1


def test_discover_two_distinct_sequences():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([
        ("dbo", "OrderNumberSequence"),
        ("Sales", "InvoiceNumberSequence"),
    ])

    sequences.discover(connection, result, seen_edges=set())

    names = {obj.name for obj in result.sequences}
    assert names == {"dbo.OrderNumberSequence", "Sales.InvoiceNumberSequence"}


def test_discover_does_not_touch_dependencies():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("dbo", "OrderNumberSequence")])

    sequences.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_sequences_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "sequences" in names
