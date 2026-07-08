"""
Tests for Discovery Enhancement 5: per-object-category output files, and
discovery_manifest.json remaining backward-compatible (assembled from
those files rather than replaced by them).
"""
from __future__ import annotations

import csv
import json

from autovista.output_writer import ENTITY_OUTPUT_FILES, write_csv_rollup, write_entity_outputs, write_manifest_json
from autovista.schema import (
    ConstraintEntity,
    DatabaseEntity,
    DiscoveryLogEntry,
    DiscoveryManifest,
    SchemaEntity,
    TableEntity,
)


def _sample_manifest() -> DiscoveryManifest:
    manifest = DiscoveryManifest()
    manifest.databases = [DatabaseEntity(name="SalesDW", size_mb=100.0, table_count=1, proc_count=0, view_count=0)]
    manifest.tables = [
        TableEntity(database="SalesDW", schema="dbo", name="Orders", row_count=10, size_mb=1.0, column_count=2)
    ]
    manifest.constraints = [
        ConstraintEntity(database="SalesDW", schema="dbo", table="Orders", name="PK_Orders", constraint_type="PRIMARY_KEY", columns=["OrderId"]),
        ConstraintEntity(
            database="SalesDW", schema="dbo", table="Orders", name="FK_Orders_Customers", constraint_type="FOREIGN_KEY",
            columns=["CustomerId"], referenced_table="dbo.Customers", referenced_columns=["CustomerId"],
        ),
    ]
    return manifest


def test_write_entity_outputs_creates_one_file_per_manifest_category(tmp_path):
    manifest = _sample_manifest()
    paths = write_entity_outputs(manifest, str(tmp_path))

    for field_name, filename in ENTITY_OUTPUT_FILES.items():
        assert (tmp_path / filename).exists(), f"missing output file for {field_name}"
        assert paths[field_name] == tmp_path / filename


def test_write_entity_outputs_tables_file_matches_manifest_content(tmp_path):
    manifest = _sample_manifest()
    write_entity_outputs(manifest, str(tmp_path))

    with open(tmp_path / "tables.json", encoding="utf-8") as f:
        tables = json.load(f)
    assert len(tables) == 1
    assert tables[0]["name"] == "Orders"


def test_write_entity_outputs_produces_derived_foreign_keys_file(tmp_path):
    manifest = _sample_manifest()
    paths = write_entity_outputs(manifest, str(tmp_path))

    assert "foreign_keys" in paths
    with open(paths["foreign_keys"], encoding="utf-8") as f:
        foreign_keys = json.load(f)
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["name"] == "FK_Orders_Customers"

    # constraints.json stays the complete/authoritative list (PK + FK).
    with open(tmp_path / "constraints.json", encoding="utf-8") as f:
        all_constraints = json.load(f)
    assert len(all_constraints) == 2


def test_write_manifest_json_is_assembled_from_individual_files(tmp_path):
    manifest = _sample_manifest()
    manifest_path = write_manifest_json(manifest, str(tmp_path))

    assert manifest_path == tmp_path / "discovery_manifest.json"
    with open(manifest_path, encoding="utf-8") as f:
        assembled = json.load(f)

    with open(tmp_path / "tables.json", encoding="utf-8") as f:
        tables_file_content = json.load(f)
    assert assembled["tables"] == tables_file_content

    with open(tmp_path / "constraints.json", encoding="utf-8") as f:
        constraints_file_content = json.load(f)
    assert assembled["constraints"] == constraints_file_content


def test_write_manifest_json_still_has_every_original_manifest_field(tmp_path):
    manifest = _sample_manifest()
    manifest_path = write_manifest_json(manifest, str(tmp_path))

    with open(manifest_path, encoding="utf-8") as f:
        assembled = json.load(f)

    expected_keys = {f.name for f in DiscoveryManifest.__dataclass_fields__.values()}
    assert expected_keys == set(assembled.keys())


def test_write_manifest_json_matches_manifest_to_dict_exactly(tmp_path):
    manifest = _sample_manifest()
    expected = json.loads(json.dumps(manifest.to_dict(), default=str))

    manifest_path = write_manifest_json(manifest, str(tmp_path))
    with open(manifest_path, encoding="utf-8") as f:
        assembled = json.load(f)

    assert assembled == expected


def test_write_entity_outputs_writes_schemas_file(tmp_path):
    """Added for Lakebridge Discovery parity -- schemas is a new manifest
    category (see sql_metadata_extractor.QUERY_SCHEMAS)."""
    manifest = _sample_manifest()
    manifest.schemas = [SchemaEntity(database="SalesDW", name="dbo"), SchemaEntity(database="SalesDW", name="staging")]

    paths = write_entity_outputs(manifest, str(tmp_path))

    assert "schemas" in paths
    with open(tmp_path / "schemas.json", encoding="utf-8") as f:
        schemas = json.load(f)
    assert len(schemas) == 2
    assert {s["name"] for s in schemas} == {"dbo", "staging"}


def test_write_csv_rollup_schema_count_matches_manifest(tmp_path):
    manifest = _sample_manifest()
    manifest.schemas = [SchemaEntity(database="SalesDW", name="dbo")]

    rollup_path = write_csv_rollup(manifest, str(tmp_path))

    with open(rollup_path, newline="", encoding="utf-8") as f:
        rows = {row["object_type"]: row for row in csv.DictReader(f)}
    assert int(rows["schema"]["count"]) == 1


def test_write_csv_rollup_error_row_omitted_when_log_entries_not_passed(tmp_path):
    """Backward compatibility: existing callers that don't pass log_entries
    must keep working exactly as before -- no "error" row appears."""
    manifest = _sample_manifest()
    rollup_path = write_csv_rollup(manifest, str(tmp_path))

    with open(rollup_path, newline="", encoding="utf-8") as f:
        rows = [row["object_type"] for row in csv.DictReader(f)]
    assert "error" not in rows


def test_write_csv_rollup_error_row_counts_failed_log_entries():
    import tempfile

    manifest = _sample_manifest()
    log_entries = [
        DiscoveryLogEntry(object_type="trigger", object_name="X", status="failed", error="boom"),
        DiscoveryLogEntry(object_type="table", object_name="Y", status="success"),
    ]
    with tempfile.TemporaryDirectory() as tmp_dir:
        rollup_path = write_csv_rollup(manifest, tmp_dir, log_entries=log_entries)
        with open(rollup_path, newline="", encoding="utf-8") as f:
            rows = {row["object_type"]: row for row in csv.DictReader(f)}
    assert int(rows["error"]["count"]) == 1
