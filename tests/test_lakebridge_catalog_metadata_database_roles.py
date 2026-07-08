"""
Tests for lakebridge_discovery.catalog_metadata.database_roles -- pure
object-inventory discovery from sys.database_principals only. Exercised
against a stub connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import database_roles
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


def test_discover_emits_fixed_and_custom_roles():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("db_datareader", True), ("AppRole", False)])

    database_roles.discover(connection, result, seen_edges=set())

    assert len(result.database_roles) == 2
    by_name = {r.name: r for r in result.database_roles}
    assert by_name["db_datareader"].is_fixed_role is True
    assert by_name["db_datareader"].principal_type == "ROLE"
    assert by_name["AppRole"].is_fixed_role is False


def test_discover_deduplicates_identical_names():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("AppRole", False), ("AppRole", False)])

    database_roles.discover(connection, result, seen_edges=set())

    assert len(result.database_roles) == 1


def test_discover_no_rows_is_a_noop():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([])

    database_roles.discover(connection, result, seen_edges=set())

    assert result.database_roles == []


def test_discover_does_not_touch_dependencies_or_users():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("AppRole", False)])

    database_roles.discover(connection, result, seen_edges=set())

    assert result.dependencies == []
    assert result.database_users == []


def test_database_roles_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "database_roles" in names
