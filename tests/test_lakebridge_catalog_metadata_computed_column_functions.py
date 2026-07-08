"""
Tests for lakebridge_discovery.catalog_metadata.computed_column_functions --
Table -> Function dependency discovery for computed-column and
default-constraint expressions, from sys.computed_columns/
sys.default_constraints/sys.sql_expression_dependencies only. Exercised
against a stub connection/cursor (no real SQL Server), same spirit as the
other probe tests in this package.
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import computed_column_functions
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult, LakebridgeObjectRef


class _FakeCursor:
    def __init__(self, computed_column_rows, default_constraint_rows):
        self._computed_column_rows = computed_column_rows
        self._default_constraint_rows = default_constraint_rows
        self._rows: list[tuple] = []

    def execute(self, sql: str):
        # Dispatch on which of the two queries ran -- "sys.default_constraints"
        # only appears in the default-constraint query.
        self._rows = self._default_constraint_rows if "sys.default_constraints" in sql else self._computed_column_rows
        return self

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, computed_column_rows=(), default_constraint_rows=()):
        self._computed_column_rows = computed_column_rows
        self._default_constraint_rows = default_constraint_rows

    def cursor(self):
        return _FakeCursor(self._computed_column_rows, self._default_constraint_rows)


def _base_result() -> LakebridgeDiscoveryResult:
    result = LakebridgeDiscoveryResult()
    result.tables = [LakebridgeObjectRef(object_type="table", name="Sales.Customer", source_tech="MS SQL Server")]
    return result


def test_discover_emits_computed_column_to_function_edge():
    result = _base_result()
    connection = _FakeConnection(computed_column_rows=[("Sales", "Customer", "dbo", "ufnLeadingZeros")])

    computed_column_functions.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1
    edge = result.dependencies[0]
    assert edge.source_object == "Sales.Customer"
    assert edge.target_object == "dbo.ufnleadingzeros"
    assert edge.relationship_type == "calls"
    assert edge.source_type == "table"
    assert edge.target_type == "function"
    assert edge.discovery_method == "catalog_metadata"
    assert edge.raw_category == "sys.sql_expression_dependencies"
    assert edge.resolved is True


def test_discover_emits_default_constraint_to_function_edge():
    result = _base_result()
    connection = _FakeConnection(default_constraint_rows=[("Sales", "Customer", "dbo", "ufnGetAccountingStartDate")])

    computed_column_functions.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1
    edge = result.dependencies[0]
    assert edge.source_object == "Sales.Customer"
    assert edge.target_object == "dbo.ufngetaccountingstartdate"
    assert edge.relationship_type == "calls"
    assert edge.raw_category == "sys.sql_expression_dependencies"


def test_discover_runs_both_sub_queries_together():
    result = _base_result()
    connection = _FakeConnection(
        computed_column_rows=[("Sales", "Customer", "dbo", "ufnLeadingZeros")],
        default_constraint_rows=[("Sales", "Customer", "dbo", "ufnGetAccountingStartDate")],
    )

    computed_column_functions.discover(connection, result, seen_edges=set())

    targets = {d.target_object for d in result.dependencies}
    assert targets == {"dbo.ufnleadingzeros", "dbo.ufngetaccountingstartdate"}


def test_discover_collapses_repeated_rows_to_one_edge():
    """What SELECT DISTINCT already prevents at the SQL level (e.g. two
    computed columns on the same table calling the same function) --
    exercised here as the seen_edges defense-in-depth."""
    result = _base_result()
    connection = _FakeConnection(computed_column_rows=[
        ("Sales", "Customer", "dbo", "ufnLeadingZeros"),
        ("Sales", "Customer", "dbo", "ufnLeadingZeros"),
    ])

    computed_column_functions.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1


def test_discover_falls_back_to_catalog_casing_when_table_not_yet_in_inventory():
    result = LakebridgeDiscoveryResult()  # empty tables inventory
    connection = _FakeConnection(computed_column_rows=[("Production", "Product", "dbo", "ufnGetProductStandardCost")])

    computed_column_functions.discover(connection, result, seen_edges=set())

    assert result.dependencies[0].source_object == "Production.Product"
    assert result.dependencies[0].target_object == "dbo.ufngetproductstandardcost"


def test_discover_does_not_duplicate_an_edge_already_known_from_a_prior_pass():
    result = _base_result()
    existing = LakebridgeDependencyRef(
        source_object="Sales.Customer", target_object="dbo.ufnleadingzeros", relationship_type="calls",
        source_type="table", target_type="function", discovery_method="catalog_metadata", resolved=True,
    )
    result.dependencies.append(existing)
    seen_edges = {("Sales.Customer", "dbo.ufnleadingzeros", "calls")}
    connection = _FakeConnection(computed_column_rows=[("Sales", "Customer", "dbo", "ufnLeadingZeros")])

    computed_column_functions.discover(connection, result, seen_edges)

    assert result.dependencies == [existing]


def test_discover_with_no_matching_rows_emits_nothing():
    """Represents the "expression only uses built-ins / no function" case:
    the query's INNER JOIN to sys.objects simply returns no rows, since a
    built-in call never has a referenced_id row to join against."""
    result = _base_result()
    connection = _FakeConnection()

    computed_column_functions.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_computed_column_functions_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "computed_column_functions" in names
