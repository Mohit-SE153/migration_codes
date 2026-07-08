"""
Tests for autovista.output_writer's Lakebridge Discovery parity wiring:
unsupported_objects/dependency_stats/warnings/errors -- confirms
write_entity_outputs() writes one JSON file per new field and
write_csv_rollup() gets new unsupported_object/warning rows, mirroring
tests/test_output_writer_entity_files.py's existing conventions.

Uses tempfile.TemporaryDirectory() rather than the tmp_path fixture -- same
workaround already used by
test_output_writer_entity_files.py::test_write_csv_rollup_error_row_counts_failed_log_entries
on this dev machine (tmp_path's own cleanup-lock machinery can
intermittently collide with a local filesystem-minifilter/AV interaction).
"""
from __future__ import annotations

import csv
import json
import tempfile

from autovista.output_writer import ENTITY_OUTPUT_FILES, write_csv_rollup, write_entity_outputs, write_manifest_json
from autovista.schema import DiscoveryManifest, UnsupportedObjectEntity


def _manifest_with_parity_fields() -> DiscoveryManifest:
    manifest = DiscoveryManifest()
    manifest.unsupported_objects = [
        UnsupportedObjectEntity(object_type="stored_procedure", name="dbo.usp_Weird", parse_status="unresolved", reason="dynamic SQL"),
    ]
    manifest.dependency_stats = {"total_dependencies": 3, "by_relationship_type": {"reads": 3}}
    manifest.warnings = ["stored_procedure:dbo.usp_Weird: dynamic SQL"]
    manifest.errors = ["trigger:dbo.trgBad: parse exploded"]
    return manifest


def test_entity_output_files_includes_parity_fields():
    assert ENTITY_OUTPUT_FILES["unsupported_objects"] == "unsupported_objects.json"
    assert ENTITY_OUTPUT_FILES["dependency_stats"] == "dependency_stats.json"
    assert ENTITY_OUTPUT_FILES["warnings"] == "warnings.json"
    assert ENTITY_OUTPUT_FILES["errors"] == "errors.json"


def test_write_entity_outputs_writes_parity_json_files():
    manifest = _manifest_with_parity_fields()
    with tempfile.TemporaryDirectory() as tmp_dir:
        paths = write_entity_outputs(manifest, tmp_dir)

        with open(paths["unsupported_objects"], encoding="utf-8") as f:
            unsupported = json.load(f)
        assert unsupported[0]["name"] == "dbo.usp_Weird"

        with open(paths["dependency_stats"], encoding="utf-8") as f:
            stats = json.load(f)
        assert stats["total_dependencies"] == 3

        with open(paths["warnings"], encoding="utf-8") as f:
            assert json.load(f) == ["stored_procedure:dbo.usp_Weird: dynamic SQL"]

        with open(paths["errors"], encoding="utf-8") as f:
            assert json.load(f) == ["trigger:dbo.trgBad: parse exploded"]


def test_write_manifest_json_still_has_every_field_including_parity_additions():
    manifest = _manifest_with_parity_fields()
    with tempfile.TemporaryDirectory() as tmp_dir:
        manifest_path = write_manifest_json(manifest, tmp_dir)
        with open(manifest_path, encoding="utf-8") as f:
            assembled = json.load(f)

    expected_keys = {f.name for f in DiscoveryManifest.__dataclass_fields__.values()}
    assert expected_keys == set(assembled.keys())
    assert assembled["dependency_stats"]["total_dependencies"] == 3


def test_write_csv_rollup_includes_unsupported_object_and_warning_rows():
    manifest = _manifest_with_parity_fields()
    with tempfile.TemporaryDirectory() as tmp_dir:
        rollup_path = write_csv_rollup(manifest, tmp_dir)
        with open(rollup_path, newline="", encoding="utf-8") as f:
            rows = {row["object_type"]: row for row in csv.DictReader(f)}

    assert int(rows["unsupported_object"]["count"]) == 1
    assert int(rows["warning"]["count"]) == 1


def test_write_csv_rollup_reports_zero_for_empty_parity_fields():
    manifest = DiscoveryManifest()
    with tempfile.TemporaryDirectory() as tmp_dir:
        rollup_path = write_csv_rollup(manifest, tmp_dir)
        with open(rollup_path, newline="", encoding="utf-8") as f:
            rows = {row["object_type"]: row for row in csv.DictReader(f)}

    assert int(rows["unsupported_object"]["count"]) == 0
    assert int(rows["warning"]["count"]) == 0
