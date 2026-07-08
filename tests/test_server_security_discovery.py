"""
Tests for Discovery Phase 2.5: server-level security discovery
(sys.server_principals/sys.server_role_members/sys.server_permissions/
sys.servers), which is server-scoped (called once per run) rather than
per-database, mirroring server_instance's placement (Phase 1.2).
"""
from __future__ import annotations

from autovista.sql_metadata_extractor import (
    FixtureMetadataSource,
    LiveSqlServerSource,
    extract_database_metadata,
)
from fixtures.mock_catalog import MockCatalog


class FakeCursor:
    def __init__(self, responses: list[tuple[str, list[tuple]]]):
        self._responses = responses
        self._rows: list[tuple] = []

    def execute(self, sql, *params):
        for needle, rows in self._responses:
            if needle in sql:
                self._rows = rows
                return
        self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    def __init__(self, responses: list[tuple[str, list[tuple]]]):
        self.responses = responses

    def cursor(self):
        return FakeCursor(self.responses)


def _source(responses: list[tuple[str, list[tuple]]]) -> LiveSqlServerSource:
    return LiveSqlServerSource(connection=FakeConnection(responses))


# --- Server principals + role membership ----------------------------------

def test_list_server_security_principals_maps_login_and_server_role_types():
    source = _source([
        ("FROM sys.server_role_members", []),
        ("FROM sys.server_principals sp", [
            ("sa", "S", False, False, None),
            ("sysadmin", "R", False, True, None),
        ]),
    ])
    principals = source.list_server_security_principals()
    sa = next(p for p in principals if p.name == "sa")
    role = next(p for p in principals if p.name == "sysadmin")
    assert sa.principal_type == "LOGIN"
    assert sa.scope == "server"
    assert sa.database == ""
    assert role.principal_type == "SERVER_ROLE"
    assert role.is_fixed_role is True


def test_list_server_security_principals_attaches_role_membership():
    source = _source([
        ("FROM sys.server_role_members", [
            ("sysadmin", "sa"),
            ("public", "sa"),
        ]),
        ("FROM sys.server_principals sp", [
            ("sa", "S", False, False, None),
        ]),
    ])
    principals = source.list_server_security_principals()
    sa = next(p for p in principals if p.name == "sa")
    assert sorted(sa.member_of_roles) == ["public", "sysadmin"]


def test_list_server_security_principals_with_no_role_membership_gets_empty_list():
    source = _source([
        ("FROM sys.server_role_members", []),
        ("FROM sys.server_principals sp", [
            ("guest_login", "S", False, False, None),
        ]),
    ])
    principals = source.list_server_security_principals()
    assert principals[0].member_of_roles == []


# --- Server permissions ----------------------------------------------------

def test_list_server_permissions_resolves_grantee_and_scope():
    source = _source([
        ("FROM sys.server_permissions perm", [
            ("svc_readonly", "S", "SERVER", None, "VIEW SERVER STATE", "GRANT"),
        ]),
    ])
    perms = source.list_server_permissions()
    assert len(perms) == 1
    perm = perms[0]
    assert perm.grantee == "svc_readonly"
    assert perm.scope == "server"
    assert perm.database == ""
    assert perm.permission_name == "VIEW SERVER STATE"


def test_list_server_permissions_resolves_server_principal_target_name():
    source = _source([
        ("FROM sys.server_permissions perm", [
            ("alice", "S", "SERVER_PRINCIPAL", "bob", "IMPERSONATE", "GRANT"),
        ]),
    ])
    perm = source.list_server_permissions()[0]
    assert perm.class_desc == "SERVER_PRINCIPAL"
    assert perm.object_name == "bob"


# --- Linked servers ---------------------------------------------------------

def test_list_linked_servers_redacts_password_in_provider_string():
    source = _source([
        ("FROM sys.servers", [
            ("PROD_LINK", "SQL Server", "SQLNCLI", "prod-db.internal,1433",
             "Data Source=prod-db.internal;User ID=svc;Password=SuperSecret123"),
        ]),
    ])
    linked = source.list_linked_servers()
    assert len(linked) == 1
    entry = linked[0]
    assert entry.name == "PROD_LINK"
    assert entry.data_source == "prod-db.internal,1433"
    assert "SuperSecret123" not in entry.provider_string_redacted
    assert "***REDACTED***" in entry.provider_string_redacted


def test_list_linked_servers_with_null_provider_string_stays_none():
    source = _source([
        ("FROM sys.servers", [
            ("PROD_LINK", "SQL Server", "SQLNCLI", "prod-db.internal,1433", None),
        ]),
    ])
    entry = source.list_linked_servers()[0]
    assert entry.provider_string_redacted is None


def test_list_linked_servers_empty_when_none_configured():
    source = _source([("FROM sys.servers", [])])
    assert source.list_linked_servers() == []


# --- Fixture-mode parity + end-to-end wiring --------------------------------

def test_fixture_server_security_principals_and_permissions_have_server_scope():
    source = FixtureMetadataSource(catalog=MockCatalog())
    principals = source.list_server_security_principals()
    assert principals
    assert all(p.scope == "server" for p in principals)
    sa = next(p for p in principals if p.name == "sa")
    assert "sysadmin" in sa.member_of_roles

    perms = source.list_server_permissions()
    assert perms
    assert all(p.scope == "server" for p in perms)


def test_fixture_linked_server_provider_string_is_redacted():
    source = FixtureMetadataSource(catalog=MockCatalog())
    linked = source.list_linked_servers()
    assert len(linked) == 1
    assert "***REDACTED***" in linked[0].provider_string_redacted
    assert "Fixture#Only1" not in linked[0].provider_string_redacted


def test_server_and_database_scoped_principals_are_merged_in_extract_database_metadata():
    source = FixtureMetadataSource(catalog=MockCatalog())
    result, log_entries = extract_database_metadata(source, database="SalesDW")

    scopes = {p.scope for p in result["security_principals"]}
    assert scopes == {"server", "database"}

    perm_scopes = {p.scope for p in result["permissions"]}
    assert "server" in perm_scopes

    assert result["linked_servers"]

    object_types = {e.object_type for e in log_entries}
    assert "server_security_principal" in object_types
    assert "server_permission" in object_types
    assert "linked_server" in object_types


def test_database_summary_user_and_role_counts_are_unaffected_by_server_principals():
    """database_summary.total_users/total_roles are computed BEFORE
    server-scoped principals are merged into the returned
    security_principals list (see sql_metadata_extractor.py's
    extract_database_metadata) -- server principal_type values
    ("LOGIN"/"SERVER_ROLE") never match the summary's "USER"/"ROLE"
    comparisons anyway, but this asserts the merge ordering doesn't
    accidentally inflate the database-scoped counts."""
    source = FixtureMetadataSource(catalog=MockCatalog())
    result, _ = extract_database_metadata(source, database="SalesDW")
    summary = result["database_summary"][0]
    database_scoped = [p for p in result["security_principals"] if p.scope == "database"]
    assert summary.total_users == sum(1 for p in database_scoped if p.principal_type == "USER")
    assert summary.total_roles == sum(1 for p in database_scoped if p.principal_type == "ROLE")
