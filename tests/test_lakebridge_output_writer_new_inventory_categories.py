"""
Tests for lakebridge_discovery.output_writer's indexes/constraints/sequences
wiring: confirms write_entity_outputs() writes the three new JSON files and
write_csv_rollup()'s counts match the same result object -- the exact
consistency this task's Part 2 investigation confirmed already holds for
every existing category.
"""
from __future__ import annotations

import csv
import json

from lakebridge_discovery.output_writer import ENTITY_OUTPUT_FILES, write_csv_rollup, write_entity_outputs
from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeObjectRef


def _result_with_new_categories() -> LakebridgeDiscoveryResult:
    result = LakebridgeDiscoveryResult()
    result.indexes = [
        LakebridgeObjectRef(object_type="index", name="Sales.SalesOrderHeader.PK_X", source_tech="MS SQL Server"),
    ]
    result.constraints = [
        LakebridgeObjectRef(object_type="constraint", name="Sales.PK_X", source_tech="MS SQL Server"),
        LakebridgeObjectRef(object_type="constraint", name="Sales.FK_Y", source_tech="MS SQL Server"),
    ]
    result.sequences = [
        LakebridgeObjectRef(object_type="sequence", name="dbo.OrderNumberSequence", source_tech="MS SQL Server"),
    ]
    return result


def test_entity_output_files_includes_new_categories():
    assert ENTITY_OUTPUT_FILES["indexes"] == "indexes.json"
    assert ENTITY_OUTPUT_FILES["constraints"] == "constraints.json"
    assert ENTITY_OUTPUT_FILES["sequences"] == "sequences.json"


def test_write_entity_outputs_writes_new_json_files(tmp_path):
    result = _result_with_new_categories()
    paths = write_entity_outputs(result, str(tmp_path))

    assert "indexes" in paths and "constraints" in paths and "sequences" in paths

    with open(tmp_path / "indexes.json", encoding="utf-8") as f:
        assert len(json.load(f)) == 1
    with open(tmp_path / "constraints.json", encoding="utf-8") as f:
        assert len(json.load(f)) == 2
    with open(tmp_path / "sequences.json", encoding="utf-8") as f:
        assert len(json.load(f)) == 1


def test_rollup_counts_match_json_counts_for_new_categories(tmp_path):
    """Directly exercises the exact consistency check this task's Part 2
    investigation performed by hand: rollup count == JSON file object
    count, for every newly-added category."""
    result = _result_with_new_categories()
    write_entity_outputs(result, str(tmp_path))
    write_csv_rollup(result, str(tmp_path))

    with open(tmp_path / "lakebridge_rollup.csv", newline="", encoding="utf-8") as f:
        rollup_rows = {row["object_type"]: int(row["count"]) for row in csv.DictReader(f)}

    for object_type, filename in (("index", "indexes.json"), ("constraint", "constraints.json"), ("sequence", "sequences.json")):
        with open(tmp_path / filename, encoding="utf-8") as f:
            json_count = len(json.load(f))
        assert rollup_rows[object_type] == json_count, f"{object_type}: rollup={rollup_rows[object_type]} json={json_count}"


def test_rollup_counts_are_zero_when_categories_are_empty(tmp_path):
    result = LakebridgeDiscoveryResult()
    write_csv_rollup(result, str(tmp_path))

    with open(tmp_path / "lakebridge_rollup.csv", newline="", encoding="utf-8") as f:
        rollup_rows = {row["object_type"]: int(row["count"]) for row in csv.DictReader(f)}

    assert rollup_rows["index"] == 0
    assert rollup_rows["constraint"] == 0
    assert rollup_rows["sequence"] == 0
