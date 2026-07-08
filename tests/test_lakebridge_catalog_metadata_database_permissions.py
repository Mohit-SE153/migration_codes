"""
Tests for lakebridge_discovery.catalog_metadata.database_permissions --
pure object-inventory discovery from sys.database_permissions/
sys.database_principals only. Exercised against a stub connection/cursor
(no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import database_permissions
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


def test_discover_emits_one_row_per_grant():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([
        ("AppUser", "S", "OBJECT_OR_COLUMN", "Orders", "SELECT", "GRANT"),
        ("AppRole", "R", "DATABASE", None, "CREATE TABLE", "GRANT"),
    ])

    database_permissions.discover(connection, result, seen_edges=set())

    assert len(result.database_permissions) == 2
    first = result.database_permissions[0]
    assert first.grantee == "AppUser"
    assert first.principal_type == "S"
    assert first.class_desc == "OBJECT_OR_COLUMN"
    assert first.object_name == "Orders"
    assert first.permission_name == "SELECT"
    assert first.state_desc == "GRANT"


def test_discover_no_rows_is_a_noop():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([])

    database_permissions.discover(connection, result, seen_edges=set())

    assert result.database_permissions == []


def test_discover_does_not_touch_dependencies_or_server_permissions():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("AppUser", "S", "DATABASE", None, "CONNECT", "GRANT")])

    database_permissions.discover(connection, result, seen_edges=set())

    assert result.dependencies == []
    assert result.server_permissions == []


def test_database_permissions_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "database_permissions" in names
