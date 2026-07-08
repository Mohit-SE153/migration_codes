"""
Tests for lakebridge_discovery.catalog_metadata.database_users -- pure
object-inventory discovery from sys.database_principals/sys.database_role_members
only. Exercised against a stub connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import database_users
from lakebridge_discovery.schema import LakebridgeDiscoveryResult


class _FakeCursor:
    def __init__(self, users_rows, membership_rows):
        self._users_rows = users_rows
        self._membership_rows = membership_rows
        self._rows: list[tuple] = []

    def execute(self, sql: str):
        self._rows = self._membership_rows if "database_role_members" in sql else self._users_rows
        return self

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, users_rows=(), membership_rows=()):
        self._users_rows = users_rows
        self._membership_rows = membership_rows

    def cursor(self):
        return _FakeCursor(self._users_rows, self._membership_rows)


def test_discover_emits_database_user_with_role_membership():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(
        users_rows=[("AppUser", 5)],
        membership_rows=[(5, "db_datareader"), (5, "db_datawriter")],
    )

    database_users.discover(connection, result, seen_edges=set())

    assert len(result.database_users) == 1
    user = result.database_users[0]
    assert user.name == "AppUser"
    assert user.principal_type == "USER"
    assert set(user.member_of_roles) == {"db_datareader", "db_datawriter"}


def test_discover_handles_user_with_no_role_membership():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(users_rows=[("dbo", 1)])

    database_users.discover(connection, result, seen_edges=set())

    assert result.database_users[0].member_of_roles == []


def test_discover_deduplicates_identical_names():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(users_rows=[("AppUser", 5), ("AppUser", 5)])

    database_users.discover(connection, result, seen_edges=set())

    assert len(result.database_users) == 1


def test_discover_no_rows_is_a_noop():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(users_rows=[])

    database_users.discover(connection, result, seen_edges=set())

    assert result.database_users == []


def test_discover_does_not_touch_dependencies_or_roles():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(users_rows=[("AppUser", 5)])

    database_users.discover(connection, result, seen_edges=set())

    assert result.dependencies == []
    assert result.database_roles == []


def test_database_users_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "database_users" in names
