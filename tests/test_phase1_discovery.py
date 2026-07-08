"""
Tests for Discovery Phase 1 (critical gaps closed):
  1. Table structural flags (temporal/memory-optimized/CDC/change-tracking/
     partitioning/compression) via LiveSqlServerSource.list_tables.
  2. Server/instance-level discovery (ServerInstanceEntity), including
     graceful degradation when permission-sensitive DMVs are unavailable.
  3. Proc/function parameter extraction (sys.parameters).

Follows the FakeCursor/FakeConnection convention established in
test_constraint_discovery.py / test_live_sql_metadata_extractor.py.
"""
from __future__ import annotations

from autovista.data_quality_analyzer import build_data_quality_summary
from autovista.sql_lineage_parser import enrich_trigger
from autovista.sql_metadata_extractor import FixtureMetadataSource, LiveSqlServerSource, extract_database_metadata
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


class ExplodingConnection(FakeConnection):
    """Raises for any query whose SQL text contains one of `blocked_needles`
    -- used to simulate a permission-restricted DMV, mirroring
    test_live_sql_metadata_extractor.py's ExplodingDmvConnection pattern."""

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


def _source(responses: list[tuple[str, list[tuple]]]) -> LiveSqlServerSource:
    return LiveSqlServerSource(connection=FakeConnection(responses))


# --- 1. Table structural flags -----------------------------------------

def test_list_tables_populates_temporal_memory_optimized_cdc_and_change_tracking():
    source = _source([
        ("JOIN sys.master_files mf", [("SalesDW", 1000.0)]),
        ("sys.change_tracking_tables", [
            ("dbo", "Orders", 2, False, True, 0),  # system-versioned temporal + CDC
            ("dbo", "OrderDetails", 0, True, False, 1),  # memory-optimized + change tracking
        ]),
        ("FROM sys.partitions p", []),
        ("FROM sys.tables t", [
            ("dbo", "Orders", "2024-01-01", "2024-06-01", "CLUSTERED", 100, 10.0, 0, 0, 0, 0, 0, 1, 1, 1),
            ("dbo", "OrderDetails", "2024-01-01", "2024-06-01", "CLUSTERED", 200, 5.0, 0, 0, 0, 0, 0, 1, 1, 1),
        ]),
        ("FROM sys.columns c", []),
    ])
    tables = source.list_tables("SalesDW")
    orders = next(t for t in tables if t.name == "Orders")
    details = next(t for t in tables if t.name == "OrderDetails")

    assert orders.is_temporal_table is True
    assert orders.is_cdc_enabled is True
    assert orders.is_memory_optimized is False
    assert orders.is_change_tracking_enabled is False

    assert details.is_memory_optimized is True
    assert details.is_change_tracking_enabled is True
    assert details.is_temporal_table is False
    assert details.is_cdc_enabled is False


def test_list_tables_partition_count_and_compression_single_partition():
    source = _source([
        ("JOIN sys.master_files mf", [("SalesDW", 1000.0)]),
        ("sys.change_tracking_tables", []),
        ("FROM sys.partitions p", [
            ("dbo", "Orders", 1, "PAGE"),
        ]),
        ("FROM sys.tables t", [
            ("dbo", "Orders", "2024-01-01", "2024-06-01", "CLUSTERED", 100, 10.0, 0, 0, 0, 0, 0, 1, 1, 1),
        ]),
        ("FROM sys.columns c", []),
    ])
    orders = source.list_tables("SalesDW")[0]
    assert orders.partition_count == 1
    assert orders.is_partitioned is False
    assert orders.compression == "PAGE"


def test_list_tables_reports_mixed_compression_across_partitions_explicitly():
    source = _source([
        ("JOIN sys.master_files mf", [("SalesDW", 1000.0)]),
        ("sys.change_tracking_tables", []),
        ("FROM sys.partitions p", [
            ("dbo", "Orders", 1, "PAGE"),
            ("dbo", "Orders", 2, "NONE"),
            ("dbo", "Orders", 3, "ROW"),
        ]),
        ("FROM sys.tables t", [
            ("dbo", "Orders", "2024-01-01", "2024-06-01", "CLUSTERED", 100, 10.0, 0, 0, 0, 0, 0, 1, 1, 1),
        ]),
        ("FROM sys.columns c", []),
    ])
    orders = source.list_tables("SalesDW")[0]
    assert orders.partition_count == 3
    assert orders.is_partitioned is True
    assert orders.compression == "MIXED (NONE, PAGE, ROW)"


def test_list_tables_defaults_structural_flags_when_no_feature_row_matches():
    source = _source([
        ("JOIN sys.master_files mf", [("SalesDW", 1000.0)]),
        ("sys.change_tracking_tables", []),
        ("FROM sys.partitions p", []),
        ("FROM sys.tables t", [
            ("dbo", "Lonely", "2024-01-01", "2024-06-01", "CLUSTERED", 1, 1.0, 0, 0, 0, 0, 0, 1, 1, 1),
        ]),
        ("FROM sys.columns c", []),
    ])
    lonely = source.list_tables("SalesDW")[0]
    assert lonely.is_temporal_table is False
    assert lonely.is_memory_optimized is False
    assert lonely.is_cdc_enabled is False
    assert lonely.is_change_tracking_enabled is False
    assert lonely.is_partitioned is False
    assert lonely.partition_count == 0
    assert lonely.compression is None


# --- 2. Server/instance-level discovery ---------------------------------

def test_list_server_instance_reads_serverproperty_and_sys_info():
    source = _source([
        ("SERVERPROPERTY", [
            ("16.0.4255.1", "RTM", "Developer Edition (64-bit)", 3, "SQLHOST01", "PROD"),
        ]),
        ("FROM sys.dm_os_sys_info", [(12, 26672640)]),
        ("FROM sys.configurations", [(2147483647,)]),
    ])
    entity = source.list_server_instance()
    assert entity.product_version == "16.0.4255.1"
    assert entity.edition == "Developer Edition (64-bit)"
    assert entity.engine_edition == 3
    assert entity.machine_name == "SQLHOST01"
    assert entity.instance_name == "PROD"
    assert entity.cpu_count == 12
    assert entity.physical_memory_mb == round(26672640 / 1024.0, 2)
    assert entity.max_server_memory_mb == 2147483647


def test_list_server_instance_tolerates_permission_denied_dmvs():
    source = LiveSqlServerSource(connection=ExplodingConnection(
        responses=[
            ("SERVERPROPERTY", [
                ("16.0.4255.1", "RTM", "Developer Edition (64-bit)", 3, "SQLHOST01", None),
            ]),
        ],
        blocked_needles=["sys.dm_os_sys_info", "sys.configurations"],
    ))
    entity = source.list_server_instance()
    assert entity.product_version == "16.0.4255.1"
    assert entity.instance_name is None
    # DMV/configuration fields unavailable -- left at defaults, not fatal.
    assert entity.cpu_count is None
    assert entity.physical_memory_mb is None
    assert entity.max_server_memory_mb is None


def test_list_server_instance_returns_none_when_serverproperty_query_empty():
    source = _source([])
    assert source.list_server_instance() is None


def test_fixture_server_instance_has_plausible_values():
    source = FixtureMetadataSource(catalog=MockCatalog())
    entity = source.list_server_instance()
    assert entity is not None
    assert entity.product_version
    assert entity.cpu_count and entity.cpu_count > 0
    assert entity.physical_memory_mb and entity.physical_memory_mb > 0


def test_server_instance_is_wired_into_extract_database_metadata_and_manifest():
    source = FixtureMetadataSource(catalog=MockCatalog())
    result, log_entries = extract_database_metadata(source, database="SalesDW")
    assert result["server_instance"] is not None
    assert result["server_instance"].product_version

    entry = next(e for e in log_entries if e.object_type == "server_instance")
    assert entry.status == "success"
    assert entry.parse_status == "direct_metadata"


# --- 3. Proc/function parameter extraction ------------------------------

def test_list_procedures_populates_parameters_excluding_return_value_row():
    source = _source([
        ("FROM sys.parameters p", [
            (123, 1, "@CutoffDate", "datetime2", False),
        ]),
        ("FROM sys.procedures p", [
            ("dbo", "usp_ArchiveOldOrders", "CREATE PROCEDURE dbo.usp_ArchiveOldOrders @CutoffDate DATETIME2 AS SELECT 1",
             "2024-01-01", "2024-06-01", 0, None, 123),
        ]),
    ])
    procs = source.list_procedures("SalesDW")
    entity, _ = procs[0]
    assert entity.parameter_count == 1
    assert entity.parameters[0].name == "@CutoffDate"
    assert entity.parameters[0].data_type == "datetime2"
    assert entity.parameters[0].mode == "IN"


def test_list_procedures_marks_output_parameters_as_out_mode():
    source = _source([
        ("FROM sys.parameters p", [
            (123, 1, "@InputVal", "int", False),
            (123, 2, "@OutputVal", "int", True),
        ]),
        ("FROM sys.procedures p", [
            ("dbo", "usp_WithOutput", "CREATE PROCEDURE dbo.usp_WithOutput @InputVal INT, @OutputVal INT OUTPUT AS SELECT 1",
             "2024-01-01", "2024-06-01", 0, None, 123),
        ]),
    ])
    procs = source.list_procedures("SalesDW")
    entity, _ = procs[0]
    assert entity.parameter_count == 2
    modes = {p.name: p.mode for p in entity.parameters}
    assert modes["@InputVal"] == "IN"
    assert modes["@OutputVal"] == "OUT"


def test_list_procedures_with_no_parameters_gets_empty_list_not_crash():
    source = _source([
        ("FROM sys.parameters p", []),
        ("FROM sys.procedures p", [
            ("dbo", "usp_NoParams", "CREATE PROCEDURE dbo.usp_NoParams AS SELECT 1",
             "2024-01-01", "2024-06-01", 0, None, 999),
        ]),
    ])
    procs = source.list_procedures("SalesDW")
    entity, _ = procs[0]
    assert entity.parameter_count == 0
    assert entity.parameters == []


def test_list_functions_populates_parameters_from_shared_parameter_query():
    source = _source([
        ("FROM sys.parameters p", [
            (456, 1, "@OrderId", "int", False),
        ]),
        ("FROM sys.objects f", [
            ("dbo", "ufn_GetOrderStatus", "SQL_SCALAR_FUNCTION", "nvarchar", 456,
             "CREATE FUNCTION dbo.ufn_GetOrderStatus(@OrderId INT) RETURNS nvarchar AS BEGIN RETURN '' END"),
        ]),
    ])
    functions = source.list_functions("SalesDW")
    entity, _ = functions[0]
    assert entity.parameter_count == 1
    assert entity.parameters[0].name == "@OrderId"
    assert entity.return_type == "nvarchar"


def test_fixture_procedures_and_functions_expose_real_parameters():
    source = FixtureMetadataSource(catalog=MockCatalog())
    procs = {p.name: p for p, _ in source.list_procedures("SalesDW")}
    assert procs["usp_ArchiveOldOrders"].parameter_count == 1
    assert procs["usp_ArchiveOldOrders"].parameters[0].name == "CutoffDate"
    assert procs["usp_LoadCustomersFromStaging"].parameter_count == 0

    functions = {f.name: f for f, _ in source.list_functions("SalesDW")}
    assert functions["ufn_GetOrderStatus"].parameter_count == 1
    assert functions["ufn_GetOrderStatus"].parameters[0].name == "OrderId"


# --- End-to-end: data_quality_analyzer counters against updated fixture data --

# --- Regression: MockTrigger.definition (fixture-mode trigger AttributeError) --

def test_fixture_list_triggers_exposes_definition_text_no_attribute_error():
    """MockTrigger previously had no `definition` field, so
    FixtureMetadataSource.list_triggers() accessing `t.definition` raised
    AttributeError on every fixture-mode run (see fixtures/mock_catalog.py
    MockTrigger / MockCatalog._handle_trigger). Guards against a regression
    of that crash and confirms the definition text is the real CREATE
    TRIGGER batch, not a placeholder."""
    source = FixtureMetadataSource(catalog=MockCatalog())
    triggers = source.list_triggers("SalesDW")
    assert triggers, "fixture DDL must define at least one trigger"
    entity, definition = triggers[0]
    assert entity.name == "trg_Orders_UpdateModifiedDate"
    assert definition
    assert "CREATE TRIGGER" in definition.upper()


def test_fixture_trigger_definition_enriches_referenced_tables_via_lineage_parser():
    """End-to-end: the real fixture trigger body (an UPDATE joining back to
    dbo.Orders) must flow all the way through enrich_trigger() and produce
    real referenced_tables -- not just direct-metadata fields (schema/name/
    table/event) -- now that MockTrigger.definition is populated."""
    source = FixtureMetadataSource(catalog=MockCatalog())
    entity, definition = next(
        (e, d) for e, d in source.list_triggers("SalesDW") if e.name == "trg_Orders_UpdateModifiedDate"
    )
    enrich_trigger(entity, definition)
    assert entity.parse_status in ("sqlglot", "direct_metadata")
    assert "dbo.Orders" in entity.referenced_tables


def test_extract_database_metadata_reports_trigger_success_not_failed():
    """Full extraction path (as orchestrator.py drives it): a trigger with
    a real, non-empty definition must log as success, mirroring the
    fixture-mode run's discovery_log_summary.csv having zero `failed` rows
    for object_type=trigger."""
    source = FixtureMetadataSource(catalog=MockCatalog())
    _, log_entries = extract_database_metadata(source, database="SalesDW")
    trigger_entries = [e for e in log_entries if e.object_type == "trigger"]
    assert trigger_entries
    assert all(e.status != "failed" for e in trigger_entries)


def test_data_quality_summary_counts_structural_flags_from_updated_fixture_tables():
    source = FixtureMetadataSource(catalog=MockCatalog())
    tables = source.list_tables("SalesDW")
    summary = build_data_quality_summary("SalesDW", tables, indexes=[], constraints=[])
    assert summary.tables_with_cdc_enabled >= 1
    assert summary.tables_with_change_tracking_enabled >= 1
    assert summary.tables_with_temporal_tables >= 1
