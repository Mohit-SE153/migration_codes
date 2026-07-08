"""
Tests for Discovery Phase 2.6: wiring sql_lineage_parser.py's existing
DYNAMIC_SQL_MARKERS detection (which previously fired but was discarded)
into a reusable LineageResult.dynamic_sql_detected field, and from there
into StoredProcedureEntity.dynamic_sql_usage via enrich_stored_procedure().
"""
from __future__ import annotations

from autovista.schema import StoredProcedureEntity
from autovista.sql_lineage_parser import enrich_stored_procedure, parse_lineage
from autovista.sql_metadata_extractor import FixtureMetadataSource
from fixtures.mock_catalog import MockCatalog


def test_dynamic_sql_detected_true_on_the_early_return_path():
    r = parse_lineage("EXEC sp_executesql @sql")
    assert r.parse_status == "unresolved"
    assert r.dynamic_sql_detected is True


def test_dynamic_sql_detected_true_for_exec_paren_variable_form():
    r = parse_lineage("EXEC (@sql)")
    assert r.dynamic_sql_detected is True


def test_dynamic_sql_detected_false_for_clean_static_sql():
    r = parse_lineage("SELECT * FROM dbo.Customers")
    assert r.parse_status == "sqlglot"
    assert r.dynamic_sql_detected is False


def test_dynamic_sql_detected_false_for_unresolved_garbage_input():
    """parse_status == 'unresolved' does NOT always mean dynamic SQL --
    a genuine sqlglot parse error is a different, unrelated reason to be
    unresolved. dynamic_sql_detected must stay False in that case rather
    than being conflated with "unresolved"."""
    r = parse_lineage("this is not %%% valid t-sql at !! all (((")
    assert r.parse_status == "unresolved"
    assert r.dynamic_sql_detected is False


def test_dynamic_sql_marker_anywhere_in_text_is_still_caught():
    sql = """
    CREATE PROCEDURE dbo.usp_Mixed @TableName SYSNAME AS
    BEGIN
        DECLARE @sql NVARCHAR(MAX) = N'SELECT * FROM ' + QUOTENAME(@TableName);
        EXEC sp_executesql @sql;
    END
    """
    r = parse_lineage(sql)
    assert r.dynamic_sql_detected is True


def test_enrich_stored_procedure_wires_dynamic_sql_detected_into_entity_field():
    proc = StoredProcedureEntity(database="SalesDW", schema="dbo", name="usp_DynamicReportBuilder", loc=5)
    definition = (
        "CREATE PROCEDURE dbo.usp_DynamicReportBuilder @TableName SYSNAME AS "
        "BEGIN DECLARE @sql NVARCHAR(MAX); "
        "SET @sql = N'SELECT * FROM ' + QUOTENAME(@TableName); "
        "EXEC sp_executesql @sql; END"
    )
    enrich_stored_procedure(proc, definition)
    assert proc.dynamic_sql_usage is True
    assert proc.parse_status == "unresolved"


def test_enrich_stored_procedure_leaves_dynamic_sql_usage_false_for_static_proc():
    proc = StoredProcedureEntity(database="SalesDW", schema="dbo", name="usp_Static", loc=3)
    definition = "CREATE PROCEDURE dbo.usp_Static AS BEGIN SELECT * FROM dbo.Orders; END"
    enrich_stored_procedure(proc, definition)
    assert proc.dynamic_sql_usage is False


def test_fixture_dynamic_report_builder_proc_is_correctly_flagged_after_enrichment():
    """The fixture DDL's one deliberately-dynamic-SQL proc
    (usp_DynamicReportBuilder, fixtures/sql/ddl_sample.sql) should end up
    dynamic_sql_usage=True once actually run through enrich_stored_procedure
    -- confirming the wiring works end-to-end against real fixture SQL
    text, not just a synthetic string."""
    source = FixtureMetadataSource(catalog=MockCatalog())
    procs = {p.name: (p, definition) for p, definition in source.list_procedures("SalesDW")}
    proc, definition = procs["usp_DynamicReportBuilder"]
    enrich_stored_procedure(proc, definition)
    assert proc.dynamic_sql_usage is True


def test_fixture_non_dynamic_procs_still_report_false_after_enrichment():
    """Ground-rule check from the task brief: fixture procs that don't use
    dynamic SQL must still report dynamic_sql_usage=False once run through
    the now-wired enrichment -- their hardcoded fixture default (False)
    should be unaffected because they genuinely contain no dynamic SQL
    markers, not because the field is dead."""
    source = FixtureMetadataSource(catalog=MockCatalog())
    for proc, definition in source.list_procedures("SalesDW"):
        if proc.name == "usp_DynamicReportBuilder":
            continue
        enrich_stored_procedure(proc, definition)
        assert proc.dynamic_sql_usage is False, f"{proc.name} unexpectedly flagged as dynamic SQL"
