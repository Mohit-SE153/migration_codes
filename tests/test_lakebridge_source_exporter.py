"""
Tests for lakebridge_discovery.source_exporter's supplementary catalog
metadata (server instance, table structural flags, proc/function
parameters, server-level security, linked servers) -- all gathered over
this module's own independent pyodbc connection, retyped independently of
autovista/sql_metadata_extractor.py's equivalent queries per this
codebase's no-shared-parsing/query-logic rule between the two Discovery
engines.

Follows the FakeCursor/FakeConnection convention established in
tests/test_phase1_discovery.py / tests/test_live_sql_metadata_extractor.py
-- no real database needed for the live-path tests.
"""
from __future__ import annotations

from lakebridge_discovery.config import LakebridgeConfig, SqlServerConfig
from lakebridge_discovery.schema import LakebridgeDiscoveryResult
from lakebridge_discovery.source_exporter import (
    _fetch_linked_servers,
    _fetch_procedure_parameters,
    _fetch_server_instance,
    _fetch_server_permissions,
    _fetch_server_principals,
    _fetch_table_features,
    _populate_fixture_supplementary_metadata,
    export_supplementary_metadata,
)


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


class ExplodingConnection(FakeConnection):
    """Raises for any query whose SQL text contains one of `blocked_needles`
    -- simulates a permission-restricted DMV/catalog view."""

    def __init__(self, responses, blocked_needles):
        super().__init__(responses)
        self.blocked_needles = blocked_needles

    def cursor(self):
        cursor = super().cursor()
        original_execute = cursor.execute

        def execute(sql, *params):
            if any(b in sql for b in self.blocked_needles):
                raise RuntimeError("permission denied")
            return original_execute(sql, *params)

        cursor.execute = execute
        return cursor


def _fixture_config(run_mode: str = "fixture", host: str = "localhost") -> LakebridgeConfig:
    return LakebridgeConfig(
        enabled=True,
        run_mode=run_mode,
        source=SqlServerConfig(
            host=host, database="SalesDW", username="sa", password="x", use_integrated_auth=False,
        ),
        dtsx_fallback_dir=None,
        cli_path="databricks",
        source_tech_sql="MS SQL Server",
        source_tech_etl="SSIS",
        generate_json=True,
        analyze_timeout_seconds=60,
        output_dir="./_test_output_lakebridge",
        source_export_dir="./_test_output_lakebridge/_source_export",
        catalog_metadata_sources="*",
    )


# --- 1. Server-instance metadata -----------------------------------------

def test_fetch_server_instance_reads_serverproperty_and_sys_info():
    connection = FakeConnection([
        ("SERVERPROPERTY", [("16.0.4255.1", "RTM", "Developer Edition (64-bit)", 3, "SQLHOST01", "PROD")]),
        ("FROM sys.dm_os_sys_info", [(12, 26672640)]),
        ("FROM sys.configurations", [(2147483647,)]),
    ])
    entity = _fetch_server_instance(connection)
    assert entity.product_version == "16.0.4255.1"
    assert entity.engine_edition == 3
    assert entity.machine_name == "SQLHOST01"
    assert entity.instance_name == "PROD"
    assert entity.cpu_count == 12
    assert entity.physical_memory_mb == round(26672640 / 1024.0, 2)
    assert entity.max_server_memory_mb == 2147483647


def test_fetch_server_instance_tolerates_permission_denied_dmvs():
    connection = ExplodingConnection(
        responses=[("SERVERPROPERTY", [("16.0.4255.1", "RTM", "Developer Edition (64-bit)", 3, "SQLHOST01", None)])],
        blocked_needles=["sys.dm_os_sys_info", "sys.configurations"],
    )
    entity = _fetch_server_instance(connection)
    assert entity.product_version == "16.0.4255.1"
    assert entity.cpu_count is None
    assert entity.physical_memory_mb is None
    assert entity.max_server_memory_mb is None


def test_fetch_server_instance_returns_none_when_serverproperty_empty():
    assert _fetch_server_instance(FakeConnection([])) is None


# --- 2. Table structural flags --------------------------------------------

def test_fetch_table_features_populates_flags_and_partition_compression():
    connection = FakeConnection([
        ("FROM sys.partitions p", [
            ("dbo", "Orders", 1, "PAGE"),
            ("dbo", "Orders", 2, "PAGE"),
        ]),
        ("FROM sys.tables t", [
            ("dbo", "Orders", 2, False, True, 0),  # system-versioned temporal + CDC
            ("dbo", "OrderDetails", 0, True, False, 1),  # memory-optimized + change tracking
        ]),
    ])
    features = _fetch_table_features(connection)
    orders = next(t for t in features if t.name == "Orders")
    details = next(t for t in features if t.name == "OrderDetails")

    assert orders.is_temporal_table is True
    assert orders.is_cdc_enabled is True
    assert orders.is_memory_optimized is False
    assert orders.partition_count == 2
    assert orders.is_partitioned is True
    assert orders.compression == "PAGE"

    assert details.is_memory_optimized is True
    assert details.is_change_tracking_enabled is True
    assert details.is_temporal_table is False
    assert details.partition_count == 0
    assert details.compression is None


def test_fetch_table_features_reports_mixed_compression_explicitly():
    connection = FakeConnection([
        ("FROM sys.partitions p", [
            ("dbo", "Orders", 1, "PAGE"),
            ("dbo", "Orders", 2, "NONE"),
        ]),
        ("FROM sys.tables t", [("dbo", "Orders", 0, False, False, 0)]),
    ])
    features = _fetch_table_features(connection)
    assert features[0].compression == "MIXED (NONE, PAGE)"


# --- 3. Procedure/function parameters --------------------------------------

def test_fetch_procedure_parameters_marks_output_mode():
    connection = FakeConnection([
        ("FROM sys.parameters p", [
            ("dbo", "usp_WithOutput", "@InputVal", "int", False),
            ("dbo", "usp_WithOutput", "@OutputVal", "int", True),
        ]),
    ])
    params = _fetch_procedure_parameters(connection)
    assert len(params) == 2
    modes = {p.parameter_name: p.mode for p in params}
    assert modes["@InputVal"] == "IN"
    assert modes["@OutputVal"] == "OUT"
    assert all(p.name == "usp_WithOutput" and p.schema == "dbo" for p in params)


def test_fetch_procedure_parameters_empty_list_not_crash():
    assert _fetch_procedure_parameters(FakeConnection([("FROM sys.parameters p", [])])) == []


# --- 4. Server-level security / linked servers ------------------------------

def test_fetch_server_principals_attaches_role_membership():
    connection = FakeConnection([
        ("sys.server_role_members", [("sysadmin", "app_login")]),
        ("FROM sys.server_principals sp", [
            ("app_login", "S", False, False),
            ("sysadmin", "R", False, True),
        ]),
    ])
    principals = _fetch_server_principals(connection)
    app_login = next(p for p in principals if p.name == "app_login")
    sysadmin = next(p for p in principals if p.name == "sysadmin")
    assert app_login.principal_type == "LOGIN"
    assert app_login.member_of_roles == ["sysadmin"]
    assert sysadmin.principal_type == "SERVER_ROLE"
    assert sysadmin.is_fixed_role is True


def test_fetch_server_permissions_maps_columns():
    connection = FakeConnection([
        ("FROM sys.server_permissions perm", [
            ("app_login", "S", "SERVER", None, "CONNECT SQL", "GRANT"),
        ]),
    ])
    perms = _fetch_server_permissions(connection)
    assert len(perms) == 1
    assert perms[0].grantee == "app_login"
    assert perms[0].permission_name == "CONNECT SQL"


def test_fetch_linked_servers_redacts_password_in_provider_string():
    connection = FakeConnection([
        ("FROM sys.servers", [
            ("REMOTE_SRV", "SQLNCLI", "SQLNCLI", "remote.example.com", "Provider=SQLNCLI;Password=hunter2;"),
        ]),
    ])
    servers = _fetch_linked_servers(connection)
    assert len(servers) == 1
    assert "hunter2" not in servers[0].provider_string_redacted
    assert "REDACTED" in servers[0].provider_string_redacted


def test_fetch_linked_servers_empty_when_none_linked():
    assert _fetch_linked_servers(FakeConnection([("FROM sys.servers", [])])) == []


# --- 5. Fixture-mode population ---------------------------------------------

def test_populate_fixture_supplementary_metadata_covers_all_fixture_tables():
    result = LakebridgeDiscoveryResult()
    _populate_fixture_supplementary_metadata(result)

    assert result.server_instance is not None
    assert "FIXTURE" in (result.server_instance.edition or "").upper()

    # fixtures/sql/ddl_sample.sql documents "21 tables (15 dbo + 6 staging)".
    assert len(result.table_features) == 21
    assert all(t.schema in ("dbo", "staging") for t in result.table_features)

    param_names = {(p.name, p.parameter_name) for p in result.procedure_parameters}
    assert ("usp_ArchiveOldOrders", "CutoffDate") in param_names

    assert result.server_principals
    assert result.linked_servers == []


def test_export_supplementary_metadata_fixture_mode_populates_result_and_logs_success():
    result = LakebridgeDiscoveryResult()
    log_entries = export_supplementary_metadata(_fixture_config("fixture"), result)

    assert result.server_instance is not None
    assert result.table_features
    assert result.procedure_parameters
    assert result.server_principals
    assert all(e.status == "success" for e in log_entries)
    object_types = {e.object_type for e in log_entries}
    assert {"server_instance", "table_features", "procedure_parameters", "server_security", "linked_servers"} <= object_types


def test_export_supplementary_metadata_live_mode_connection_failure_isolated():
    """A bad live connection must not raise -- every sub-stage is recorded
    as a failed log entry instead, same defensive style as export_source().
    Uses a deliberately unresolvable host (not "localhost", which may be a
    real reachable SQL Server in some environments this test runs in) so
    the connection attempt is guaranteed to fail."""
    config = _fixture_config("live", host="nonexistent-host-for-test.invalid")
    result = LakebridgeDiscoveryResult()
    log_entries = export_supplementary_metadata(config, result)
    assert result.server_instance is None
    assert all(e.status == "failed" for e in log_entries)
