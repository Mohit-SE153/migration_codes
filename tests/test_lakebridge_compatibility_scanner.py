"""
Tests for lakebridge_discovery.compatibility_scanner -- an independent
reimplementation of autovista/compatibility_scanner.py's detection logic
(never an import of it, per this codebase's no-shared-parsing-logic rule
between the two Discovery engines). Exercises both scan_compatibility_flags()
directly (same construct set autovista's scanner detects) and
apply_compatibility_flags() against a tmp_path standing in for
<source_export_dir>, using source_exporter.py's own {kind}__{schema}.{name}.sql
file-naming convention.
"""
from __future__ import annotations

from pathlib import Path

from lakebridge_discovery.compatibility_scanner import apply_compatibility_flags, scan_compatibility_flags
from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeObjectRef


def test_scan_detects_merge():
    sql = """
        MERGE INTO dbo.Target AS t
        USING dbo.Source AS s ON t.Id = s.Id
        WHEN MATCHED THEN UPDATE SET t.Value = s.Value
        WHEN NOT MATCHED THEN INSERT (Id, Value) VALUES (s.Id, s.Value);
    """
    assert "MERGE" in scan_compatibility_flags(sql)


def test_scan_detects_pivot_and_unpivot():
    pivot_sql = "SELECT * FROM dbo.Sales PIVOT (SUM(Amount) FOR Quarter IN ([Q1],[Q2])) AS p"
    assert "PIVOT" in scan_compatibility_flags(pivot_sql)

    unpivot_sql = "SELECT * FROM dbo.Sales UNPIVOT (Amount FOR Quarter IN ([Q1],[Q2])) AS u"
    assert "UNPIVOT" in scan_compatibility_flags(unpivot_sql)


def test_scan_detects_cross_and_outer_apply():
    cross_sql = "SELECT * FROM dbo.Orders o CROSS APPLY dbo.ufn_GetItems(o.OrderId) i"
    assert "CROSS_APPLY" in scan_compatibility_flags(cross_sql)

    outer_sql = "SELECT * FROM dbo.Orders o OUTER APPLY dbo.ufn_GetItems(o.OrderId) i"
    assert "OUTER_APPLY" in scan_compatibility_flags(outer_sql)


def test_scan_detects_openjson():
    sql = "SELECT * FROM OPENJSON(@json) WITH (Id INT '$.id')"
    assert "OPENJSON" in scan_compatibility_flags(sql)


def test_scan_detects_for_xml_and_for_json_via_regex():
    assert "FOR_XML" in scan_compatibility_flags("SELECT * FROM dbo.T FOR XML AUTO")
    assert "FOR_JSON" in scan_compatibility_flags("SELECT * FROM dbo.T FOR JSON AUTO")


def test_scan_detects_openquery_opendatasource_xp_cmdshell_sp_oa():
    assert "OPENQUERY" in scan_compatibility_flags("SELECT * FROM OPENQUERY(LinkedSrv, 'SELECT 1')")
    assert "OPENDATASOURCE" in scan_compatibility_flags("SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Server=x')")
    assert "XP_CMDSHELL" in scan_compatibility_flags("EXEC xp_cmdshell 'dir'")
    assert "SP_OA" in scan_compatibility_flags("EXEC sp_OACreate 'Excel.Application', @obj OUTPUT")


def test_scan_detects_linked_server_four_part_name():
    sql = "SELECT * FROM [RemoteServer].[RemoteDB].[dbo].[RemoteTable]"
    assert "LINKED_SERVER" in scan_compatibility_flags(sql)


def test_scan_returns_empty_for_plain_sql():
    assert scan_compatibility_flags("SELECT Id, Name FROM dbo.Customers WHERE Id = 1") == []


def test_scan_returns_empty_for_none_or_empty_text():
    assert scan_compatibility_flags(None) == []
    assert scan_compatibility_flags("") == []


def test_scan_survives_unparseable_trigger_body_via_regex_fallback():
    """CREATE TRIGGER's declaration header isn't in sqlglot's tsql grammar
    -- the AST pass alone would degrade to an opaque Command node for an
    unstripped trigger body, but the regex scan must still fire
    independently on the raw text."""
    sql = """
        CREATE TRIGGER dbo.trg_Test ON dbo.Orders AFTER UPDATE
        AS
        BEGIN
            EXEC xp_cmdshell 'whoami';
        END;
    """
    assert "XP_CMDSHELL" in scan_compatibility_flags(sql)


# --- apply_compatibility_flags: wiring against exported files --------------

def _write_sql(sql_dir: Path, filename: str, text: str) -> None:
    (sql_dir / filename).write_text(text, encoding="utf-8")


def test_apply_compatibility_flags_sets_flags_on_matching_inventory_objects(tmp_path):
    export_dir = tmp_path
    sql_dir = export_dir / "sql"
    sql_dir.mkdir()

    _write_sql(sql_dir, "view__Sales.vPivoted.sql", "CREATE VIEW Sales.vPivoted AS SELECT * FROM t PIVOT (SUM(x) FOR y IN ([a],[b])) p")
    _write_sql(sql_dir, "sql_stored_procedure__dbo.uspClean.sql", "CREATE PROCEDURE dbo.uspClean AS SELECT 1")

    result = LakebridgeDiscoveryResult()
    result.views = [LakebridgeObjectRef(object_type="view", name="Sales.vPivoted", source_tech="MS SQL Server")]
    result.stored_procedures = [LakebridgeObjectRef(object_type="stored_procedure", name="dbo.uspClean", source_tech="MS SQL Server")]

    apply_compatibility_flags(result, export_dir)

    assert result.views[0].compatibility_flags == ["PIVOT"]
    assert result.stored_procedures[0].compatibility_flags == []


def test_apply_compatibility_flags_leaves_empty_when_no_matching_file(tmp_path):
    export_dir = tmp_path
    (export_dir / "sql").mkdir()

    result = LakebridgeDiscoveryResult()
    result.tables = [LakebridgeObjectRef(object_type="table", name="dbo.Ghost", source_tech="MS SQL Server")]

    apply_compatibility_flags(result, export_dir)
    assert result.tables[0].compatibility_flags == []


def test_apply_compatibility_flags_records_warning_when_export_dir_missing(tmp_path):
    result = LakebridgeDiscoveryResult()
    apply_compatibility_flags(result, tmp_path / "does_not_exist")
    assert any("compatibility_scanner" in w for w in result.warnings)
