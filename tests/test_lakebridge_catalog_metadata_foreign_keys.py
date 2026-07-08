"""
Tests for lakebridge_discovery.catalog_metadata.foreign_keys -- Table ->
Table dependency discovery from sys.foreign_keys/sys.tables/sys.schemas
only. Exercised against a stub connection/cursor (no real SQL Server), same
spirit as the rest of this engine's tests
(test_lakebridge_dependency_extractor.py,
test_lakebridge_report_parser_native_dependencies.py).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import foreign_keys
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult, LakebridgeObjectRef


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


def _base_result() -> LakebridgeDiscoveryResult:
    result = LakebridgeDiscoveryResult()
    result.tables = [
        LakebridgeObjectRef(object_type="table", name="Sales.SalesOrderHeader", source_tech="MS SQL Server"),
        LakebridgeObjectRef(object_type="table", name="Sales.SalesTerritory", source_tech="MS SQL Server"),
    ]
    return result


def test_discover_emits_table_to_table_foreign_key_edge():
    result = _base_result()
    connection = _FakeConnection([("Sales", "SalesOrderHeader", "Sales", "SalesTerritory")])

    foreign_keys.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1
    edge = result.dependencies[0]
    assert edge.source_object == "Sales.SalesOrderHeader"
    assert edge.target_object == "sales.salesterritory"
    assert edge.relationship_type == "foreign_key"
    assert edge.source_type == "table"
    assert edge.target_type == "table"
    assert edge.discovery_method == "catalog_metadata"
    assert edge.raw_category == "sys.foreign_keys"
    assert edge.resolved is True


def test_discover_preserves_source_object_casing_from_inventory():
    """source_object must match the exact casing already in result.tables
    (mirroring how every other dependency source treats "the object being
    scanned"), even though the catalog query itself returned it as
    ("Sales", "SalesOrderHeader") independently."""
    result = _base_result()
    connection = _FakeConnection([("sales", "salesorderheader", "Sales", "SalesTerritory")])

    foreign_keys.discover(connection, result, seen_edges=set())

    assert result.dependencies[0].source_object == "Sales.SalesOrderHeader"


def test_discover_falls_back_to_catalog_casing_when_table_not_yet_in_inventory():
    result = LakebridgeDiscoveryResult()  # empty tables inventory
    connection = _FakeConnection([("Purchasing", "Vendor", "Purchasing", "ShipMethod")])

    foreign_keys.discover(connection, result, seen_edges=set())

    assert result.dependencies[0].source_object == "Purchasing.Vendor"
    assert result.dependencies[0].target_object == "purchasing.shipmethod"
    assert result.dependencies[0].resolved is True
    assert result.dependencies[0].target_type == "table"


def test_discover_retains_self_referencing_foreign_key():
    """A table with an FK referencing itself (e.g. an employee-hierarchy
    ManagerID -> BusinessEntityID constraint) must NOT be dropped as a
    self-loop -- unlike the code-lineage extractors' self-loop suppression,
    this is a real structural fact."""
    result = LakebridgeDiscoveryResult()
    result.tables = [LakebridgeObjectRef(object_type="table", name="HumanResources.Employee", source_tech="MS SQL Server")]
    connection = _FakeConnection([("HumanResources", "Employee", "HumanResources", "Employee")])

    foreign_keys.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1
    edge = result.dependencies[0]
    assert edge.source_object == "HumanResources.Employee"
    assert edge.target_object == "humanresources.employee"


def test_discover_collapses_composite_foreign_key_to_one_edge():
    """A composite multi-column FK is already one row in sys.foreign_keys --
    simulating two rows for the SAME constraint pair (e.g. if a future query
    change ever accidentally produced per-column rows) must still collapse
    to exactly one edge via seen_edges, never two."""
    result = _base_result()
    connection = _FakeConnection([
        ("Sales", "SalesOrderHeader", "Sales", "SalesTerritory"),
        ("Sales", "SalesOrderHeader", "Sales", "SalesTerritory"),
    ])

    foreign_keys.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1


def test_discover_does_not_duplicate_an_edge_already_known_from_a_prior_pass():
    result = _base_result()
    existing = LakebridgeDependencyRef(
        source_object="Sales.SalesOrderHeader", target_object="sales.salesterritory", relationship_type="foreign_key",
        source_type="table", target_type="table", discovery_method="catalog_metadata", resolved=True,
    )
    result.dependencies.append(existing)
    seen_edges = {("Sales.SalesOrderHeader", "sales.salesterritory", "foreign_key")}
    connection = _FakeConnection([("Sales", "SalesOrderHeader", "Sales", "SalesTerritory")])

    foreign_keys.discover(connection, result, seen_edges)

    assert result.dependencies == [existing]  # no duplicate appended


def test_discover_two_different_tables_produce_two_edges():
    result = _base_result()
    result.tables.append(LakebridgeObjectRef(object_type="table", name="Sales.Customer", source_tech="MS SQL Server"))
    connection = _FakeConnection([
        ("Sales", "SalesOrderHeader", "Sales", "SalesTerritory"),
        ("Sales", "Customer", "Sales", "SalesTerritory"),
    ])

    foreign_keys.discover(connection, result, seen_edges=set())

    pairs = {(d.source_object, d.target_object) for d in result.dependencies}
    assert pairs == {("Sales.SalesOrderHeader", "sales.salesterritory"), ("Sales.Customer", "sales.salesterritory")}


def test_foreign_keys_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "foreign_keys" in names
