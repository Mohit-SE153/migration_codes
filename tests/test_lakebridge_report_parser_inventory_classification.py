"""
Tests for lakebridge_discovery.report_parser's inventory-row classification
-- specifically the fix for SSIS package rows silently being dropped.

Root cause (see report_parser.py's _parse_program_name/_apply_program_inventory_rows):
SQL-side inventory rows follow source_exporter.py's "{kind}__{schema}.{name}.ext"
file-naming convention, which _parse_program_name decomposes into
(category, name). SSIS inventory rows never follow that convention -- a real
SSIS Analyzer report's rows look like {"type": "Package", "name": "Pkg_Foo", ...},
with no "__" in the name at all -- so _parse_program_name always returned None
for them, and _apply_program_inventory_rows silently skipped every row before
this fix. The fix adds a fallback: when _parse_program_name can't classify a
row by name, check the row's own "type" field instead.
"""
from __future__ import annotations

from lakebridge_discovery.report_parser import (
    _apply_program_inventory_rows,
    _classify_by_row_type,
    _parse_program_name,
    parse_json_report,
)
from lakebridge_discovery.schema import LakebridgeDiscoveryResult


def test_parse_program_name_still_handles_sql_side_naming_convention():
    """Regression guard: the fix must not break the existing, working
    SQL-side classification path."""
    assert _parse_program_name("table__Sales.Store.sql") == ("table", "Sales.Store")
    assert _parse_program_name("sql_stored_procedure__dbo.uspLogError.sql") == ("stored_procedure", "dbo.uspLogError")
    assert _parse_program_name("sql_trigger__Sales.uSalesOrderHeader.sql") == ("trigger", "Sales.uSalesOrderHeader")


def test_parse_program_name_returns_none_for_ssis_style_names():
    """SSIS package names never contain "__" -- confirmed against a real
    SSIS Analyzer report where every row's "name" is a plain package name."""
    assert _parse_program_name("Pkg_ArchiveOldData") is None


def test_classify_by_row_type_maps_package():
    assert _classify_by_row_type({"type": "Package", "name": "Pkg_ArchiveOldData"}) == "package"


def test_classify_by_row_type_returns_none_for_unknown_or_missing_type():
    assert _classify_by_row_type({"name": "Pkg_ArchiveOldData"}) is None
    assert _classify_by_row_type({"type": "SomethingElse"}) is None
    assert _classify_by_row_type({"type": 123}) is None


def test_apply_program_inventory_rows_captures_ssis_packages():
    """Direct regression test for the bug: a real SSIS report's inventory
    shape (plain name + "type": "Package") must now be captured into
    result.packages instead of silently dropped."""
    result = LakebridgeDiscoveryResult()
    rows = [
        {"name": "Pkg_ArchiveOldData", "type": "Package", "complexityLevel": "LOW"},
        {"name": "Pkg_LoadCustomers", "type": "Package", "complexityLevel": "LOW"},
    ]

    applied = _apply_program_inventory_rows(result, rows, "SSIS", name_field="name", complexity_field="complexityLevel")

    assert applied == 2
    assert len(result.packages) == 2
    names = {p.name for p in result.packages}
    assert names == {"Pkg_ArchiveOldData", "Pkg_LoadCustomers"}
    assert all(p.object_type == "package" and p.source_tech == "SSIS" for p in result.packages)


def test_apply_program_inventory_rows_sql_and_ssis_rows_together():
    """Confirms the fallback doesn't interfere with the existing, working
    SQL-side rows when both shapes appear (defensive -- real reports only
    ever contain one shape per source_tech, but the function must not
    silently favor one over the other)."""
    result = LakebridgeDiscoveryResult()
    rows = [
        {"name": "table__Sales.Store.sql", "complexityLevel": "LOW"},
        {"name": "Pkg_Master", "type": "Package", "complexityLevel": "LOW"},
    ]

    applied = _apply_program_inventory_rows(result, rows, "MS SQL Server", name_field="name", complexity_field="complexityLevel")

    assert applied == 2
    assert len(result.tables) == 1 and result.tables[0].name == "Sales.Store"
    assert len(result.packages) == 1 and result.packages[0].name == "Pkg_Master"


def test_parse_json_report_end_to_end_with_real_ssis_report_shape(tmp_path):
    """End-to-end regression test using the exact shape confirmed present in
    a real, live SSIS Analyzer report for this project (see this task's
    investigation) -- 5 packages, all "type": "Package"."""
    import json

    report = {
        "inventory": [
            {"name": "Pkg_ArchiveOldData", "type": "Package", "complexityLevel": "LOW", "sourceFile": "Pkg_ArchiveOldData.dtsx"},
            {"name": "Pkg_LoadCustomers", "type": "Package", "complexityLevel": "LOW", "sourceFile": "Pkg_LoadCustomers.dtsx"},
            {"name": "Pkg_LoadOrderDetails", "type": "Package", "complexityLevel": "LOW", "sourceFile": "Pkg_LoadOrderDetails.dtsx"},
            {"name": "Pkg_LoadOrders", "type": "Package", "complexityLevel": "LOW", "sourceFile": "Pkg_LoadOrders.dtsx"},
            {"name": "Pkg_Master", "type": "Package", "complexityLevel": "LOW", "sourceFile": "Pkg_Master.dtsx"},
        ],
        "runInfo": {"sourceTechnology": "SSIS"},
    }
    report_path = tmp_path / "lakebridge_report_SSIS.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = LakebridgeDiscoveryResult()
    parse_json_report(report_path, result, "SSIS")

    assert len(result.packages) == 5
    assert {p.name for p in result.packages} == {
        "Pkg_ArchiveOldData", "Pkg_LoadCustomers", "Pkg_LoadOrderDetails", "Pkg_LoadOrders", "Pkg_Master",
    }
    assert result.warnings == []  # no "no recognizable inventory rows" warning
