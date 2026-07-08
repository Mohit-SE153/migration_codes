"""
Tests for lakebridge_discovery.output_writer's SQLGlot/autovista parity
wiring: agent_jobs/assemblies/database_users/database_roles/
database_permissions/database_summary/data_quality_summary -- confirms
write_entity_outputs() writes one JSON file per new category and
write_csv_rollup()'s counts match the same result object, mirroring
test_lakebridge_output_writer_new_inventory_categories.py's convention for
the indexes/constraints/sequences addition.

Uses tempfile.TemporaryDirectory() rather than the tmp_path fixture --
same workaround tests/test_output_writer_entity_files.py's
test_write_csv_rollup_error_row_counts_failed_log_entries already uses on
this dev machine, where pytest's own tmp_path cleanup-lock machinery can
intermittently collide with a Windows filesystem-minifilter/AV interaction.
"""
from __future__ import annotations

import csv
import json
import tempfile

from lakebridge_discovery.output_writer import ENTITY_OUTPUT_FILES, write_csv_rollup, write_entity_outputs
from lakebridge_discovery.schema import (
    AgentJobEntity,
    DatabaseSummaryEntity,
    DataQualitySummaryEntity,
    LakebridgeDiscoveryResult,
    LakebridgeObjectRef,
    ServerPermissionEntity,
    ServerPrincipalEntity,
)


def _result_with_parity_categories() -> LakebridgeDiscoveryResult:
    result = LakebridgeDiscoveryResult()
    result.agent_jobs = [AgentJobEntity(name="Nightly ETL", enabled=True)]
    result.assemblies = [LakebridgeObjectRef(object_type="clr_assembly", name="dbo.MyAssembly", source_tech="MS SQL Server")]
    result.database_users = [ServerPrincipalEntity(name="AppUser", principal_type="USER")]
    result.database_roles = [ServerPrincipalEntity(name="db_datareader", principal_type="ROLE")]
    result.database_permissions = [ServerPermissionEntity(grantee="AppUser", principal_type="S")]
    result.database_summary = [DatabaseSummaryEntity(database="AdventureWorks2022", total_tables=5)]
    result.data_quality_summary = [DataQualitySummaryEntity(database="AdventureWorks2022", total_tables=5)]
    return result


def test_entity_output_files_includes_parity_categories():
    assert ENTITY_OUTPUT_FILES["agent_jobs"] == "agent_jobs.json"
    assert ENTITY_OUTPUT_FILES["assemblies"] == "assemblies.json"
    assert ENTITY_OUTPUT_FILES["database_users"] == "database_users.json"
    assert ENTITY_OUTPUT_FILES["database_roles"] == "database_roles.json"
    assert ENTITY_OUTPUT_FILES["database_permissions"] == "database_permissions.json"
    assert ENTITY_OUTPUT_FILES["database_summary"] == "database_summary.json"
    assert ENTITY_OUTPUT_FILES["data_quality_summary"] == "data_quality_summary.json"


def test_write_entity_outputs_writes_parity_json_files():
    result = _result_with_parity_categories()
    with tempfile.TemporaryDirectory() as tmp_dir:
        paths = write_entity_outputs(result, tmp_dir)

        for field_name in (
            "agent_jobs", "assemblies", "database_users", "database_roles",
            "database_permissions", "database_summary", "data_quality_summary",
        ):
            assert field_name in paths

        with open(paths["agent_jobs"], encoding="utf-8") as f:
            jobs = json.load(f)
        assert jobs[0]["name"] == "Nightly ETL"

        with open(paths["database_summary"], encoding="utf-8") as f:
            summary = json.load(f)
        assert summary[0]["total_tables"] == 5


def test_rollup_counts_match_json_counts_for_parity_categories():
    result = _result_with_parity_categories()
    with tempfile.TemporaryDirectory() as tmp_dir:
        entity_paths = write_entity_outputs(result, tmp_dir)
        rollup_path = write_csv_rollup(result, tmp_dir)

        with open(rollup_path, newline="", encoding="utf-8") as f:
            rollup_rows = {row["object_type"]: int(row["count"]) for row in csv.DictReader(f)}

        expected = {
            "agent_job": "agent_jobs",
            "clr_assembly": "assemblies",
            "database_user": "database_users",
            "database_role": "database_roles",
            "database_permission": "database_permissions",
            "database_summary": "database_summary",
            "data_quality_summary": "data_quality_summary",
        }
        for object_type, field_name in expected.items():
            with open(entity_paths[field_name], encoding="utf-8") as f:
                json_count = len(json.load(f))
            assert rollup_rows[object_type] == json_count, f"{object_type}: rollup={rollup_rows[object_type]} json={json_count}"


def test_rollup_counts_are_zero_when_parity_categories_are_empty():
    result = LakebridgeDiscoveryResult()
    with tempfile.TemporaryDirectory() as tmp_dir:
        rollup_path = write_csv_rollup(result, tmp_dir)

        with open(rollup_path, newline="", encoding="utf-8") as f:
            rollup_rows = {row["object_type"]: int(row["count"]) for row in csv.DictReader(f)}

    for object_type in ("agent_job", "clr_assembly", "database_user", "database_role", "database_permission", "database_summary", "data_quality_summary"):
        assert rollup_rows[object_type] == 0
