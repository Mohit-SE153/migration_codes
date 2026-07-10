"""
Tests for assessment.complexity_scorer -- per-object complexity scoring
for code objects (procs/views/functions/triggers) and tables, including
the trigger-row merge for multi-event triggers.
"""
from __future__ import annotations

from assessment.complexity_scorer import build_object_complexity, _merge_duplicate_triggers
from assessment.config import AssessmentConfig


def _config() -> AssessmentConfig:
    return AssessmentConfig()


def test_clean_proc_with_no_signals_scores_low():
    manifest = {
        "tables": [], "views": [], "functions": [], "triggers": [], "packages": [], "dependencies": [],
        "stored_procedures": [{
            "database": "db", "schema": "dbo", "name": "usp_Simple", "loc": 5,
            "referenced_tables": [], "referenced_procs": [], "referenced_functions": [], "referenced_sequences": [],
            "compatibility_flags": [], "dynamic_sql_usage": False, "parse_status": "sqlglot", "unresolved_reason": None,
        }],
    }
    results = build_object_complexity(manifest, _config())
    assert len(results) == 1
    oc = results[0]
    assert oc.complexity_tier == "Low"
    assert oc.name == "dbo.usp_Simple"
    assert oc.estimated_hours == _config().effort_rubric.low_hours


def test_unresolved_parse_status_and_flags_push_tier_up():
    manifest = {
        "tables": [], "views": [], "functions": [], "triggers": [], "packages": [], "dependencies": [],
        "stored_procedures": [{
            "database": "db", "schema": "dbo", "name": "usp_Risky", "loc": 200,
            "referenced_tables": ["a", "b", "c"], "referenced_procs": [], "referenced_functions": [], "referenced_sequences": [],
            "compatibility_flags": ["MERGE", "OPENJSON"], "dynamic_sql_usage": True,
            "parse_status": "unresolved", "unresolved_reason": None,
        }],
    }
    results = build_object_complexity(manifest, _config())
    oc = results[0]
    assert oc.complexity_tier in ("High", "Critical")
    assert "dynamic SQL usage" in " ".join(oc.scoring_reasons)
    assert any("MERGE" in r for r in oc.scoring_reasons)


def test_fan_in_fan_out_counted_from_dependencies():
    manifest = {
        "tables": [{"database": "db", "schema": "dbo", "name": "Orders", "row_count": 1, "size_mb": 1, "column_count": 3}],
        "views": [], "functions": [], "triggers": [], "packages": [],
        "stored_procedures": [
            {"database": "db", "schema": "dbo", "name": "usp_A", "loc": 1, "referenced_tables": ["dbo.Orders"],
             "referenced_procs": [], "referenced_functions": [], "referenced_sequences": [], "compatibility_flags": [],
             "parse_status": "sqlglot"},
        ],
        "dependencies": [
            {"source_object": "dbo.usp_A", "source_type": "stored_procedure", "target_object": "dbo.Orders",
             "target_type": "table", "relationship_type": "reads", "discovery_method": "sqlglot"},
        ],
    }
    results = build_object_complexity(manifest, _config())
    table = next(oc for oc in results if oc.object_type == "table")
    proc = next(oc for oc in results if oc.object_type == "stored_procedure")
    assert table.fan_in == 1
    assert proc.fan_out == 1


def test_table_with_temporal_and_cdc_scores_higher_than_plain_table():
    plain = {"database": "db", "schema": "dbo", "name": "Plain", "row_count": 1, "size_mb": 1, "column_count": 5}
    rich = {
        "database": "db", "schema": "dbo", "name": "Rich", "row_count": 1, "size_mb": 1, "column_count": 5,
        "is_temporal_table": True, "is_cdc_enabled": True, "is_memory_optimized": True,
    }
    manifest = {"tables": [plain, rich], "views": [], "functions": [], "triggers": [], "packages": [],
                "stored_procedures": [], "dependencies": []}
    results = build_object_complexity(manifest, _config())
    plain_oc = next(oc for oc in results if oc.name == "dbo.Plain")
    rich_oc = next(oc for oc in results if oc.name == "dbo.Rich")
    assert rich_oc.complexity_score > plain_oc.complexity_score


def test_merge_duplicate_triggers_collapses_per_event_rows():
    triggers = [
        {"database": "db", "schema": "Sales", "name": "iduSalesOrderDetail", "table": "SalesOrderDetail",
         "event": "INSERT", "parse_status": "sqlglot", "referenced_tables": ["Sales.T1"], "compatibility_flags": []},
        {"database": "db", "schema": "Sales", "name": "iduSalesOrderDetail", "table": "SalesOrderDetail",
         "event": "UPDATE", "parse_status": "unresolved", "referenced_tables": ["Sales.T2"], "compatibility_flags": ["MERGE"]},
        {"database": "db", "schema": "Sales", "name": "iduSalesOrderDetail", "table": "SalesOrderDetail",
         "event": "DELETE", "parse_status": "sqlglot", "referenced_tables": [], "compatibility_flags": []},
    ]
    merged = _merge_duplicate_triggers(triggers)
    assert len(merged) == 1
    row = merged[0]
    assert row["parse_status"] == "unresolved"  # escalated to worst
    assert set(row["referenced_tables"]) == {"Sales.T1", "Sales.T2"}
    assert row["compatibility_flags"] == ["MERGE"]
    assert row["_merged_events"] == ["DELETE", "INSERT", "UPDATE"]


def test_merge_duplicate_triggers_leaves_unique_triggers_untouched():
    triggers = [{"database": "db", "schema": "dbo", "name": "OnlyOne", "table": "T", "event": "INSERT", "parse_status": "sqlglot"}]
    merged = _merge_duplicate_triggers(triggers)
    assert merged == triggers


def test_build_object_complexity_scores_one_row_per_merged_trigger():
    triggers = [
        {"database": "db", "schema": "dbo", "name": "Multi", "table": "T", "event": "INSERT", "parse_status": "sqlglot",
         "referenced_tables": [], "referenced_procs": [], "referenced_functions": [], "referenced_sequences": [], "compatibility_flags": []},
        {"database": "db", "schema": "dbo", "name": "Multi", "table": "T", "event": "UPDATE", "parse_status": "sqlglot",
         "referenced_tables": [], "referenced_procs": [], "referenced_functions": [], "referenced_sequences": [], "compatibility_flags": []},
    ]
    manifest = {"tables": [], "views": [], "functions": [], "triggers": triggers, "packages": [],
                "stored_procedures": [], "dependencies": []}
    results = build_object_complexity(manifest, _config())
    assert len(results) == 1
    assert results[0].object_type == "trigger"
    assert "merged 2 per-event rows" in " ".join(results[0].scoring_reasons)


def test_embedded_sql_in_ssis_package_is_scored():
    manifest = {
        "tables": [], "views": [], "functions": [], "triggers": [], "stored_procedures": [], "dependencies": [],
        "packages": [{
            "name": "Pkg1", "tasks": [{
                "name": "Task1", "embedded_sql": [{
                    "task_name": "Dynamic Report", "task_type": "ExecuteSQL", "sql_text": "SELECT 1\nSELECT 2",
                    "referenced_tables": ["dbo.X"], "referenced_procs": [], "referenced_sequences": [],
                    "compatibility_flags": [], "parse_status": "xml_parsed",
                }],
            }],
        }],
    }
    results = build_object_complexity(manifest, _config())
    assert len(results) == 1
    assert results[0].object_type == "embedded_sql"
    assert results[0].name == "Dynamic Report"
