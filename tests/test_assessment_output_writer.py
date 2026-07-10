"""Tests for assessment.output_writer."""
from __future__ import annotations

import csv
import json

from assessment.output_writer import (
    ENTITY_OUTPUT_FILES,
    _md_cell,
    write_csv_rollup,
    write_entity_outputs,
    write_manifest_json,
    write_markdown_report,
    write_risk_register_csv,
)
from assessment.schema import AssessmentManifest, AssessmentSummary, RiskFinding


def _sample_manifest() -> AssessmentManifest:
    return AssessmentManifest(
        generated_at="2026-01-01T00:00:00Z", source_manifest_path="./output/discovery_manifest.json",
        database="TestDb",
        risk_register=[RiskFinding(object_type="table", name="dbo.T", category="X", severity="High", description="bad")],
        summary=AssessmentSummary(database="TestDb", total_objects_scored=1, complexity_tier_counts={"Low": 1}),
    )


def test_write_entity_outputs_creates_one_file_per_category(tmp_path):
    manifest = _sample_manifest()
    paths = write_entity_outputs(manifest, str(tmp_path))
    assert set(paths.keys()) == set(ENTITY_OUTPUT_FILES.keys())
    for path in paths.values():
        assert path.exists()


def test_write_manifest_json_round_trips_full_contract(tmp_path):
    manifest = _sample_manifest()
    path = write_manifest_json(manifest, str(tmp_path))
    with open(path) as f:
        data = json.load(f)
    assert data["database"] == "TestDb"
    assert data["risk_register"][0]["name"] == "dbo.T"


def test_write_csv_rollup_includes_summary_stats(tmp_path):
    manifest = _sample_manifest()
    path = write_csv_rollup(manifest, str(tmp_path))
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    categories = {(r["category"], r["key"]) for r in rows}
    assert ("total_objects_scored", "(all)") in categories
    assert ("complexity_tier", "Low") in categories


def test_write_risk_register_csv_writes_one_row_per_finding(tmp_path):
    manifest = _sample_manifest()
    path = write_risk_register_csv(manifest, str(tmp_path))
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["name"] == "dbo.T"


def test_md_cell_strips_ansi_codes_and_collapses_newlines():
    raw = "line one\nline two \x1b[4mhighlighted\x1b[0m end"
    cleaned = _md_cell(raw)
    assert "\n" not in cleaned
    assert "\x1b" not in cleaned
    assert "line one line two highlighted end" == cleaned


def test_md_cell_escapes_pipe_characters():
    assert _md_cell("a | b") == "a \\| b"


def test_md_cell_truncates_long_text():
    cleaned = _md_cell("x" * 1000)
    assert len(cleaned) <= 300
    assert cleaned.endswith("...")


def test_write_markdown_report_produces_valid_single_line_table_rows(tmp_path):
    manifest = _sample_manifest()
    manifest.risk_register = [RiskFinding(
        object_type="trigger", name="dbo.Trg", category="PARSE_UNRESOLVED", severity="High",
        description="multi\nline\nerror \x1b[4mtext\x1b[0m",
    )]
    path = write_markdown_report(manifest, str(tmp_path))
    content = path.read_text()
    table_rows = [line for line in content.splitlines() if line.startswith("| trigger")]
    assert len(table_rows) == 1
    assert "\x1b" not in content


def test_write_markdown_report_handles_empty_sections(tmp_path):
    manifest = AssessmentManifest(database="EmptyDb", summary=AssessmentSummary(database="EmptyDb"))
    path = write_markdown_report(manifest, str(tmp_path))
    content = path.read_text()
    assert "No risk-register findings." in content
    assert "No migration waves" in content
