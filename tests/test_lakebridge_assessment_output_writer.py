"""Tests for lakebridge_assessment.output_writer."""
from __future__ import annotations

import json

from lakebridge_assessment.output_writer import (
    ENTITY_OUTPUT_FILES,
    _md_cell,
    write_entity_outputs,
    write_manifest_json,
    write_markdown_report,
)
from lakebridge_assessment.schema import AssessmentManifest, AssessmentSummary, RiskFinding


def _sample_manifest() -> AssessmentManifest:
    return AssessmentManifest(
        generated_at="2026-01-01T00:00:00Z", source_manifest_path="./output_lakebridge/lakebridge_manifest.json",
        database="TestDb", mapping_verified=False, mapping_notes="unverified mapping",
        risk_register=[RiskFinding(object_type="table", name="dbo.T", category="X", severity="High", description="bad")],
        summary=AssessmentSummary(database="TestDb", total_objects_scored=1, complexity_tier_counts={"Low": 1}),
    )


def test_write_entity_outputs_creates_one_file_per_category(tmp_path):
    manifest = _sample_manifest()
    paths = write_entity_outputs(manifest, str(tmp_path))
    assert set(paths.keys()) == set(ENTITY_OUTPUT_FILES.keys())


def test_write_manifest_json_includes_complexity_source_and_mapping_notes(tmp_path):
    manifest = _sample_manifest()
    path = write_manifest_json(manifest, str(tmp_path))
    with open(path) as f:
        data = json.load(f)
    assert data["complexity_source"] == "lakebridge_native"
    assert data["mapping_notes"] == "unverified mapping"


def test_md_cell_strips_ansi_and_newlines():
    assert _md_cell("a\nb \x1b[4mc\x1b[0m") == "a b c"


def test_write_markdown_report_includes_mapping_notes_blockquote(tmp_path):
    manifest = _sample_manifest()
    path = write_markdown_report(manifest, str(tmp_path))
    content = path.read_text()
    assert "unverified mapping" in content
    assert "lakebridge_native" in content
