"""
Tests for lakebridge_assessment.orchestrator. One test runs against this
project's real Lakebridge Discovery output (./output_lakebridge/lakebridge_manifest.json)
when present -- skipped otherwise (e.g. a fresh clone before Lakebridge
Discovery has ever been run).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lakebridge_assessment.config import AssessmentConfig
from lakebridge_assessment.orchestrator import run_assessment

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_LAKEBRIDGE_MANIFEST = _REPO_ROOT / "output_lakebridge" / "lakebridge_manifest.json"


def _write_sample_manifest(path: Path) -> None:
    manifest = {
        "databases": [{"name": "SampleDb"}],
        "tables": [{"name": "dbo.Orders", "complexity": "LOW"}],
        "views": [], "functions": [], "triggers": [],
        "stored_procedures": [{"name": "dbo.usp_LoadOrders", "complexity": "MEDIUM", "compatibility_flags": []}],
        "dependencies": [{"source_object": "dbo.usp_LoadOrders", "target_object": "dbo.orders"}],
        "unsupported_objects": [],
        "data_quality_summary": [{"database": "SampleDb", "heap_tables": 1}],
        "table_features": [],
        "server_principals": [{"name": "sa", "member_of_roles": ["sysadmin"]}],
        "server_permissions": [], "database_permissions": [], "linked_servers": [], "assemblies": [],
        "mapping_verified": False, "mapping_notes": "test mapping notes",
    }
    path.write_text(json.dumps(manifest))


def test_run_assessment_end_to_end_writes_all_outputs(tmp_path):
    input_path = tmp_path / "lakebridge_manifest.json"
    output_dir = tmp_path / "output_lakebridge_assessment"
    _write_sample_manifest(input_path)

    config = AssessmentConfig(input_manifest_path=str(input_path), output_dir=str(output_dir))
    manifest = run_assessment(config)

    assert manifest.database == "SampleDb"
    assert manifest.complexity_source == "lakebridge_native"
    assert manifest.mapping_notes == "test mapping notes"
    assert len(manifest.object_complexity) == 2
    assert len(manifest.migration_waves) == 2

    for filename in (
        "lakebridge_assessment_manifest.json", "lakebridge_assessment_rollup.csv", "risk_register.csv",
        "object_complexity.csv", "migration_waves.csv", "lakebridge_assessment_report.md", "lakebridge_assessment_run.log",
    ):
        assert (output_dir / filename).exists(), f"missing {filename}"


def test_run_assessment_raises_informative_error_when_input_missing(tmp_path):
    config = AssessmentConfig(input_manifest_path=str(tmp_path / "missing.json"), output_dir=str(tmp_path / "out"))
    with pytest.raises(FileNotFoundError, match="Lakebridge Discovery manifest not found"):
        run_assessment(config)


@pytest.mark.skipif(not _REAL_LAKEBRIDGE_MANIFEST.exists(), reason="requires a real Lakebridge Discovery run's output")
def test_run_assessment_against_real_lakebridge_output(tmp_path):
    config = AssessmentConfig(input_manifest_path=str(_REAL_LAKEBRIDGE_MANIFEST), output_dir=str(tmp_path / "out"))
    manifest = run_assessment(config)

    assert manifest.database
    assert len(manifest.object_complexity) > 0
    assert manifest.summary is not None
    assert manifest.summary.total_estimated_hours > 0
    # every scored object actually had a native Lakebridge complexity value
    assert all(oc.complexity_tier in ("Low", "Medium", "High", "Critical") for oc in manifest.object_complexity)
