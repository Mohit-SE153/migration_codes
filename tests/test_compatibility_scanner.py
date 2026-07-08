"""
Tests for Discovery Enhancement (Phase 2.4): the SQL-Server-feature
compatibility scanner (autovista/compatibility_scanner.py), plus its
wiring into schema.py's compatibility_flags fields and
output_writer.py's discovery_rollup.csv rollup.
"""
from __future__ import annotations

import csv
import io

from autovista.compatibility_scanner import scan_compatibility_flags
from autovista.output_writer import write_csv_rollup
from autovista.schema import DiscoveryManifest, FunctionEntity, StoredProcedureEntity, TriggerEntity, ViewEntity


# --- AST-based detection -------------------------------------------------

def test_detects_pivot_via_dedicated_ast_node():
    sql = (
        "SELECT * FROM (SELECT ProductId, Quantity FROM Inventory) src "
        "PIVOT (SUM(Quantity) FOR ProductId IN ([1],[2])) p"
    )
    assert scan_compatibility_flags(sql) == ["PIVOT"]


def test_detects_unpivot_distinctly_from_pivot():
    sql = (
        "SELECT * FROM (SELECT ProductId, Quantity FROM Inventory) src "
        "UNPIVOT (Quantity FOR ProductId IN ([1],[2])) p"
    )
    assert scan_compatibility_flags(sql) == ["UNPIVOT"]


def test_detects_cross_apply_and_outer_apply_distinctly():
    cross_sql = "SELECT * FROM Orders o CROSS APPLY (SELECT TOP 1 * FROM OrderDetails d WHERE d.OrderId = o.OrderId) x"
    outer_sql = "SELECT * FROM Orders o OUTER APPLY (SELECT TOP 1 * FROM OrderDetails d WHERE d.OrderId = o.OrderId) x"
    assert scan_compatibility_flags(cross_sql) == ["CROSS_APPLY"]
    assert scan_compatibility_flags(outer_sql) == ["OUTER_APPLY"]


def test_detects_merge_as_a_standalone_statement():
    sql = "MERGE dbo.Customers AS tgt USING staging.stg_Customers AS src ON tgt.CustomerId = src.CustomerId WHEN MATCHED THEN UPDATE SET tgt.CustomerName = src.CustomerName;"
    assert scan_compatibility_flags(sql) == ["MERGE"]


def test_detects_merge_nested_inside_a_stored_procedure_body():
    """Regression test: MERGE inside `CREATE PROCEDURE ... BEGIN ... MERGE
    ... END` parses as a nested exp.Merge node inside the CREATE
    statement's Block body -- the top-level parsed statement is
    exp.Create, not exp.Merge itself. An isinstance(stmt, exp.Merge)
    check on the top-level statement alone (the first implementation of
    this scanner) misses this entirely, which is exactly the common
    real-world shape (found via a fixture-mode orchestrator run against
    usp_LoadCustomersFromStaging, whose MERGE was silently going
    undetected)."""
    sql = (
        "CREATE PROCEDURE dbo.usp_LoadCustomersFromStaging AS BEGIN "
        "MERGE dbo.Customers AS tgt USING staging.stg_Customers AS src "
        "ON tgt.CustomerId = src.CustomerId "
        "WHEN MATCHED THEN UPDATE SET tgt.CustomerName = src.CustomerName; "
        "END;"
    )
    assert scan_compatibility_flags(sql) == ["MERGE"]


def test_detects_openjson_via_dedicated_ast_node():
    sql = "SELECT * FROM OPENJSON(@json) WITH (Id INT '$.id')"
    assert scan_compatibility_flags(sql) == ["OPENJSON"]


def test_detects_linked_server_four_part_name_via_ast():
    sql = "SELECT * FROM LinkedServer.OtherDB.dbo.SomeTable"
    assert scan_compatibility_flags(sql) == ["LINKED_SERVER"]


def test_detects_merge_inside_a_trigger_body_despite_unparseable_header():
    """CREATE TRIGGER isn't in sqlglot's tsql grammar at all -- without
    the trigger-header-stripping preprocessing this scanner borrows from
    sql_lineage_parser.py's approach, the whole statement degrades to an
    opaque Command node and every AST-based flag (MERGE/PIVOT/CROSS_APPLY/
    OPENJSON/LINKED_SERVER, none of which have a regex fallback) would be
    silently missed for every trigger."""
    sql = (
        "CREATE TRIGGER dbo.trg_x ON dbo.Orders AFTER UPDATE AS BEGIN "
        "MERGE dbo.Foo AS tgt USING dbo.Bar AS src ON tgt.Id = src.Id "
        "WHEN MATCHED THEN UPDATE SET tgt.X = src.X; "
        "END"
    )
    assert scan_compatibility_flags(sql) == ["MERGE"]


def test_option_hint_clause_does_not_block_ast_detection():
    """A proc with an OPTION(...) query-hint clause would otherwise fail
    to parse at all under sqlglot's tsql grammar (same gap
    sql_lineage_parser.py's _OPTION_HINT_CLAUSE stripping works around) --
    verify the scanner still finds a MERGE elsewhere in the same body."""
    sql = (
        "CREATE PROCEDURE dbo.usp_x AS BEGIN "
        "MERGE dbo.Foo AS tgt USING dbo.Bar AS src ON tgt.Id = src.Id "
        "WHEN MATCHED THEN UPDATE SET tgt.X = src.X; "
        "SELECT * FROM dbo.Foo OPTION (MAXRECURSION 25); "
        "END"
    )
    assert "MERGE" in scan_compatibility_flags(sql)


# --- Regex-based detection ------------------------------------------------

def test_detects_for_xml_and_for_json_clauses():
    assert scan_compatibility_flags("SELECT * FROM Orders FOR XML AUTO") == ["FOR_XML"]
    assert scan_compatibility_flags("SELECT * FROM Orders FOR JSON AUTO") == ["FOR_JSON"]


def test_detects_openquery_and_opendatasource():
    assert "OPENQUERY" in scan_compatibility_flags("SELECT * FROM OPENQUERY(LinkedSrv, 'SELECT * FROM foo')")
    assert "OPENDATASOURCE" in scan_compatibility_flags(
        "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Server=Foo').db.dbo.Bar"
    )


def test_detects_xp_cmdshell_and_sp_oa_calls():
    assert scan_compatibility_flags("EXEC xp_cmdshell 'dir'") == ["XP_CMDSHELL"]
    assert scan_compatibility_flags("EXEC sp_OACreate 'Excel.Application', @obj OUTPUT") == ["SP_OA"]


def test_plain_sql_with_no_flagged_constructs_returns_empty_list():
    assert scan_compatibility_flags("SELECT * FROM dbo.Orders WHERE OrderId = 1") == []


def test_empty_or_none_sql_text_returns_empty_list_without_crashing():
    assert scan_compatibility_flags("") == []
    assert scan_compatibility_flags(None) == []


def test_multiple_distinct_flags_are_all_reported_sorted():
    sql = "EXEC xp_cmdshell 'dir'; SELECT * FROM Orders FOR XML AUTO;"
    assert scan_compatibility_flags(sql) == ["FOR_XML", "XP_CMDSHELL"]


# --- schema.py wiring: compatibility_flags default to empty ---------------

def test_compatibility_flags_default_to_empty_list_on_every_entity_type():
    assert StoredProcedureEntity(database="d", schema="s", name="n", loc=1).compatibility_flags == []
    assert ViewEntity(database="d", schema="s", name="n").compatibility_flags == []
    assert FunctionEntity(database="d", schema="s", name="n", function_type="SCALAR").compatibility_flags == []
    assert TriggerEntity(database="d", schema="s", name="n", table="t", event="AFTER INSERT").compatibility_flags == []


# --- output_writer.py rollup ----------------------------------------------

def test_csv_rollup_includes_one_row_per_distinct_compatibility_flag(tmp_path):
    manifest = DiscoveryManifest()
    manifest.stored_procedures = [
        StoredProcedureEntity(database="d", schema="dbo", name="p1", loc=1, compatibility_flags=["MERGE"]),
        StoredProcedureEntity(database="d", schema="dbo", name="p2", loc=1, compatibility_flags=["MERGE", "PIVOT"]),
    ]
    manifest.views = [
        ViewEntity(database="d", schema="dbo", name="v1", compatibility_flags=["CROSS_APPLY"]),
    ]

    out_path = write_csv_rollup(manifest, str(tmp_path))
    with open(out_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    flag_rows = {r["object_name"]: r["count"] for r in rows if r["object_type"] == "compatibility_flag"}
    assert flag_rows == {"MERGE": "2", "PIVOT": "1", "CROSS_APPLY": "1"}


def test_csv_rollup_has_no_compatibility_flag_rows_when_nothing_flagged(tmp_path):
    manifest = DiscoveryManifest()
    out_path = write_csv_rollup(manifest, str(tmp_path))
    with open(out_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert not [r for r in rows if r["object_type"] == "compatibility_flag"]
