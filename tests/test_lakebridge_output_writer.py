"""
Tests for lakebridge_discovery.output_writer's supplementary-metadata
output files (server_instance.json, table_features.json,
procedure_parameters.json, server_security.json, linked_servers.json) and
the compatibility-flag / supplementary-metadata rows added to
write_csv_rollup -- mirrors tests/test_output_writer_entity_files.py's
convention for autovista's equivalent test.
"""
from __future__ import annotations

import csv
import json

from lakebridge_discovery.output_writer import ENTITY_OUTPUT_FILES, write_csv_rollup, write_entity_outputs
from lakebridge_discovery.schema import (
    LakebridgeDiscoveryResult,
    LakebridgeObjectRef,
    LinkedServerEntity,
    ProcedureParameterEntity,
    ServerInstanceEntity,
    ServerPermissionEntity,
    ServerPrincipalEntity,
    TableFeatureEntity,
)


def _sample_result() -> LakebridgeDiscoveryResult:
    result = LakebridgeDiscoveryResult()
    result.server_instance = ServerInstanceEntity(product_version="16.0.4255.1", machine_name="TESTHOST")
    result.table_features = [
        TableFeatureEntity(schema="dbo", name="Orders", is_temporal_table=True, partition_count=2),
    ]
    result.procedure_parameters = [
        ProcedureParameterEntity(schema="dbo", name="usp_Test", parameter_name="Id", data_type="int"),
    ]
    result.server_principals = [ServerPrincipalEntity(name="sa", principal_type="LOGIN")]
    result.server_permissions = [ServerPermissionEntity(grantee="sa", principal_type="S")]
    result.linked_servers = [LinkedServerEntity(name="REMOTE1")]
    result.views = [
        LakebridgeObjectRef(object_type="view", name="dbo.vTest", source_tech="MS SQL Server", compatibility_flags=["PIVOT"]),
    ]
    result.stored_procedures = [
        LakebridgeObjectRef(object_type="stored_procedure", name="dbo.uspTest", source_tech="MS SQL Server", compatibility_flags=["PIVOT", "MERGE"]),
    ]
    return result


def test_entity_output_files_mapping_includes_new_supplementary_categories():
    assert ENTITY_OUTPUT_FILES["server_instance"] == "server_instance.json"
    assert ENTITY_OUTPUT_FILES["table_features"] == "table_features.json"
    assert ENTITY_OUTPUT_FILES["procedure_parameters"] == "procedure_parameters.json"
    assert ENTITY_OUTPUT_FILES["linked_servers"] == "linked_servers.json"


def test_write_entity_outputs_writes_all_new_json_files(tmp_path):
    result = _sample_result()
    paths = write_entity_outputs(result, str(tmp_path))

    assert (tmp_path / "server_instance.json").exists()
    assert (tmp_path / "table_features.json").exists()
    assert (tmp_path / "procedure_parameters.json").exists()
    assert (tmp_path / "server_security.json").exists()
    assert (tmp_path / "linked_servers.json").exists()
    assert "server_security" in paths

    with open(tmp_path / "server_instance.json") as f:
        assert json.load(f)["product_version"] == "16.0.4255.1"

    with open(tmp_path / "table_features.json") as f:
        table_features = json.load(f)
        assert table_features[0]["name"] == "Orders"
        assert table_features[0]["is_temporal_table"] is True

    with open(tmp_path / "server_security.json") as f:
        security = json.load(f)
        assert security["server_principals"][0]["name"] == "sa"
        assert security["server_permissions"][0]["grantee"] == "sa"

    with open(tmp_path / "linked_servers.json") as f:
        assert json.load(f)[0]["name"] == "REMOTE1"


def test_write_csv_rollup_includes_supplementary_metadata_and_compatibility_flag_rows(tmp_path):
    result = _sample_result()
    out_path = write_csv_rollup(result, str(tmp_path))

    with open(out_path, newline="") as f:
        rows = list(csv.DictReader(f))

    by_type_name = {(r["object_type"], r["object_name"]): int(r["count"]) for r in rows}
    assert by_type_name[("server_instance", "(all)")] == 1
    assert by_type_name[("table_feature", "(all)")] == 1
    assert by_type_name[("procedure_parameter", "(all)")] == 1
    assert by_type_name[("server_principal", "(all)")] == 1
    assert by_type_name[("server_permission", "(all)")] == 1
    assert by_type_name[("linked_server", "(all)")] == 1

    # 2 objects (view + stored_procedure) both flag PIVOT; only the
    # stored_procedure flags MERGE.
    assert by_type_name[("compatibility_flag", "PIVOT")] == 2
    assert by_type_name[("compatibility_flag", "MERGE")] == 1


def test_write_csv_rollup_reports_zero_server_instance_when_none(tmp_path):
    result = LakebridgeDiscoveryResult()
    out_path = write_csv_rollup(result, str(tmp_path))
    with open(out_path, newline="") as f:
        rows = list(csv.DictReader(f))
    by_type_name = {(r["object_type"], r["object_name"]): int(r["count"]) for r in rows}
    assert by_type_name[("server_instance", "(all)")] == 0
