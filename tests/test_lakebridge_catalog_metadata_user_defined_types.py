"""
Tests for lakebridge_discovery.catalog_metadata.user_defined_types -- Table
-> UDT and Procedure/Function -> UDT dependency discovery from
sys.columns/sys.parameters/sys.types/sys.schemas only. Exercised against a
stub connection/cursor (no real SQL Server), same spirit as
test_lakebridge_catalog_metadata_foreign_keys.py.
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import user_defined_types
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult, LakebridgeObjectRef


class _FakeCursor:
    def __init__(self, table_udt_rows, routine_udt_rows, type_inventory_rows):
        self._table_udt_rows = table_udt_rows
        self._routine_udt_rows = routine_udt_rows
        self._type_inventory_rows = type_inventory_rows
        self._rows: list[tuple] = []

    def execute(self, sql: str):
        # Dispatch on which of the three queries ran -- "sys.parameters"
        # only appears in the routine-UDT query, "sys.columns" only in the
        # table-UDT query; the type-inventory query has neither.
        if "sys.parameters" in sql:
            self._rows = self._routine_udt_rows
        elif "sys.columns" in sql:
            self._rows = self._table_udt_rows
        else:
            self._rows = self._type_inventory_rows
        return self

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, table_udt_rows=(), routine_udt_rows=(), type_inventory_rows=()):
        self._table_udt_rows = table_udt_rows
        self._routine_udt_rows = routine_udt_rows
        self._type_inventory_rows = type_inventory_rows

    def cursor(self):
        return _FakeCursor(self._table_udt_rows, self._routine_udt_rows, self._type_inventory_rows)


def _base_result() -> LakebridgeDiscoveryResult:
    result = LakebridgeDiscoveryResult()
    result.tables = [LakebridgeObjectRef(object_type="table", name="Person.Person", source_tech="MS SQL Server")]
    result.stored_procedures = [
        LakebridgeObjectRef(object_type="stored_procedure", name="HumanResources.uspUpdateEmployeeHireInfo", source_tech="MS SQL Server"),
    ]
    result.functions = [
        LakebridgeObjectRef(object_type="function", name="dbo.ufnGetContactInformation", source_tech="MS SQL Server"),
    ]
    return result


def test_discover_emits_table_to_udt_edge():
    result = _base_result()
    connection = _FakeConnection(table_udt_rows=[("Person", "Person", "dbo", "NameStyle")])

    user_defined_types.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1
    edge = result.dependencies[0]
    assert edge.source_object == "Person.Person"
    assert edge.target_object == "dbo.namestyle"
    assert edge.relationship_type == "uses_type"
    assert edge.source_type == "table"
    assert edge.target_type == "user_defined_type"
    assert edge.discovery_method == "catalog_metadata"
    assert edge.raw_category == "sys.columns+sys.types"
    assert edge.resolved is True


def test_discover_collapses_multiple_same_typed_columns_to_one_edge():
    """Simulates what real SELECT DISTINCT already prevents at the SQL
    level -- two columns of the same UDT on the same table must still
    collapse to one edge via seen_edges, defense in depth."""
    result = _base_result()
    connection = _FakeConnection(table_udt_rows=[
        ("Person", "Person", "dbo", "NameStyle"),
        ("Person", "Person", "dbo", "NameStyle"),
    ])

    user_defined_types.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1


def test_discover_emits_stored_procedure_to_udt_edge():
    result = _base_result()
    connection = _FakeConnection(routine_udt_rows=[
        ("HumanResources", "uspUpdateEmployeeHireInfo", "SQL_STORED_PROCEDURE", "dbo", "Flag"),
    ])

    user_defined_types.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1
    edge = result.dependencies[0]
    assert edge.source_object == "HumanResources.uspUpdateEmployeeHireInfo"
    assert edge.target_object == "dbo.flag"
    assert edge.source_type == "stored_procedure"
    assert edge.target_type == "user_defined_type"
    assert edge.raw_category == "sys.parameters+sys.types"


def test_discover_emits_function_to_udt_edge_for_each_function_kind():
    result = _base_result()
    for type_desc in ("SQL_SCALAR_FUNCTION", "SQL_TABLE_VALUED_FUNCTION", "SQL_INLINE_TABLE_VALUED_FUNCTION"):
        result.dependencies = []
        connection = _FakeConnection(routine_udt_rows=[
            ("dbo", "ufnGetContactInformation", type_desc, "dbo", "Flag"),
        ])
        user_defined_types.discover(connection, result, seen_edges=set())
        assert len(result.dependencies) == 1
        assert result.dependencies[0].source_type == "function"


def test_discover_skips_unrecognized_object_type_desc_defensively():
    """The WHERE clause already restricts to known routine kinds -- this
    covers the defensive branch in case a future SQL Server version's
    type_desc value isn't in the mapping yet."""
    result = _base_result()
    connection = _FakeConnection(routine_udt_rows=[
        ("dbo", "SomeNewRoutineKind", "SQL_SOMETHING_UNKNOWN", "dbo", "Flag"),
    ])

    user_defined_types.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_discover_falls_back_to_catalog_casing_when_object_not_yet_in_inventory():
    result = LakebridgeDiscoveryResult()  # empty inventory
    connection = _FakeConnection(
        table_udt_rows=[("Sales", "SalesOrderHeader", "dbo", "OrderNumber")],
        routine_udt_rows=[("dbo", "uspLogError", "SQL_STORED_PROCEDURE", "dbo", "Flag")],
    )

    user_defined_types.discover(connection, result, seen_edges=set())

    sources = {d.source_object for d in result.dependencies}
    assert sources == {"Sales.SalesOrderHeader", "dbo.uspLogError"}


def test_discover_does_not_duplicate_an_edge_already_known_from_a_prior_pass():
    result = _base_result()
    existing = LakebridgeDependencyRef(
        source_object="Person.Person", target_object="dbo.namestyle", relationship_type="uses_type",
        source_type="table", target_type="user_defined_type", discovery_method="catalog_metadata", resolved=True,
    )
    result.dependencies.append(existing)
    seen_edges = {("Person.Person", "dbo.namestyle", "uses_type")}
    connection = _FakeConnection(table_udt_rows=[("Person", "Person", "dbo", "NameStyle")])

    user_defined_types.discover(connection, result, seen_edges)

    assert result.dependencies == [existing]


def test_discover_runs_both_table_and_routine_sub_queries_together():
    result = _base_result()
    connection = _FakeConnection(
        table_udt_rows=[("Person", "Person", "dbo", "NameStyle")],
        routine_udt_rows=[("HumanResources", "uspUpdateEmployeeHireInfo", "SQL_STORED_PROCEDURE", "dbo", "Flag")],
    )

    user_defined_types.discover(connection, result, seen_edges=set())

    pairs = {(d.source_object, d.target_object, d.source_type) for d in result.dependencies}
    assert pairs == {
        ("Person.Person", "dbo.namestyle", "table"),
        ("HumanResources.uspUpdateEmployeeHireInfo", "dbo.flag", "stored_procedure"),
    }


def test_user_defined_types_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "user_defined_types" in names


def test_discover_populates_type_inventory_separately_from_dependency_edges():
    """result.user_defined_types (distinct TYPE objects) is separate from,
    and much smaller than, result.dependencies (uses_type edges) -- this is
    the parity addition: same probe, an additional inventory list, not a
    duplicate of the edge count."""
    result = _base_result()
    connection = _FakeConnection(
        table_udt_rows=[("Person", "Person", "dbo", "NameStyle"), ("Person", "Person", "dbo", "Flag")],
        type_inventory_rows=[("dbo", "NameStyle", False), ("dbo", "Flag", False), ("dbo", "OrderNumberTable", True)],
    )

    user_defined_types.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 2  # unaffected by the new inventory step
    assert len(result.user_defined_types) == 3
    names = {obj.name for obj in result.user_defined_types}
    assert names == {"dbo.NameStyle", "dbo.Flag", "dbo.OrderNumberTable"}
    notes = {obj.name: obj.notes for obj in result.user_defined_types}
    assert notes["dbo.OrderNumberTable"] == "TABLE_TYPE"
    assert notes["dbo.NameStyle"] == "ALIAS_TYPE"
    assert all(obj.object_type == "user_defined_type" and obj.raw_category == "sys.types" for obj in result.user_defined_types)


def test_discover_deduplicates_type_inventory_rows():
    result = _base_result()
    connection = _FakeConnection(type_inventory_rows=[("dbo", "Flag", False), ("dbo", "Flag", False)])

    user_defined_types.discover(connection, result, seen_edges=set())

    assert len(result.user_defined_types) == 1
