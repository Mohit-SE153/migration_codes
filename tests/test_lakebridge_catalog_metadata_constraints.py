"""
Tests for lakebridge_discovery.catalog_metadata.constraints -- pure object-
inventory discovery from sys.key_constraints/sys.check_constraints/
sys.default_constraints/sys.foreign_keys/sys.schemas only. Exercised
against a stub connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import constraints
from lakebridge_discovery.schema import LakebridgeDiscoveryResult


class _FakeCursor:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def execute(self, sql: str):
        return self

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, key_rows=(), check_rows=(), default_rows=(), fk_rows=()):
        self._key_rows = key_rows
        self._check_rows = check_rows
        self._default_rows = default_rows
        self._fk_rows = fk_rows

    def cursor(self):
        return self

    def execute(self, sql: str):
        if "sys.key_constraints" in sql:
            self._rows = self._key_rows
        elif "sys.check_constraints" in sql:
            self._rows = self._check_rows
        elif "sys.default_constraints" in sql:
            self._rows = self._default_rows
        else:
            self._rows = self._fk_rows
        return self

    def fetchall(self):
        return self._rows


def test_discover_emits_key_constraint():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(key_rows=[("Sales", "PK_SalesOrderHeader_SalesOrderID", "PRIMARY_KEY_CONSTRAINT")])

    constraints.discover(connection, result, seen_edges=set())

    assert len(result.constraints) == 1
    obj = result.constraints[0]
    assert obj.object_type == "constraint"
    assert obj.name == "Sales.PK_SalesOrderHeader_SalesOrderID"
    assert obj.raw_category == "sys.key_constraints"
    assert obj.notes == "PRIMARY_KEY_CONSTRAINT"


def test_discover_emits_all_four_constraint_kinds_together():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(
        key_rows=[("Sales", "PK_SalesOrderHeader_SalesOrderID", "PRIMARY_KEY_CONSTRAINT")],
        check_rows=[("Sales", "CK_SalesOrderHeader_Status")],
        default_rows=[("Sales", "DF_SalesOrderHeader_OrderDate")],
        fk_rows=[("Sales", "FK_SalesOrderHeader_SalesTerritory")],
    )

    constraints.discover(connection, result, seen_edges=set())

    raw_categories = {obj.raw_category for obj in result.constraints}
    assert raw_categories == {
        "sys.key_constraints", "sys.check_constraints", "sys.default_constraints", "sys.foreign_keys",
    }
    assert len(result.constraints) == 4


def test_discover_deduplicates_identical_rows_within_one_kind():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(check_rows=[
        ("Sales", "CK_SalesOrderHeader_Status"),
        ("Sales", "CK_SalesOrderHeader_Status"),
    ])

    constraints.discover(connection, result, seen_edges=set())

    assert len(result.constraints) == 1


def test_discover_does_not_touch_dependencies():
    """Constraint object inventory is separate from foreign_keys.py's
    Table -> Table dependency edges -- this probe must never append to
    result.dependencies."""
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(fk_rows=[("Sales", "FK_SalesOrderHeader_SalesTerritory")])

    constraints.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_constraints_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "constraints" in names
