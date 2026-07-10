"""
Tests for assessment.orchestrator -- end-to-end wiring of every scoring/
rollup module plus output writing. One test also runs against this
project's real sqlglot Discovery output (./output/discovery_manifest.json)
when present, since that's the actual artifact this phase is meant to
consume; skipped if that file hasn't been generated (e.g. a fresh clone
before Discovery has ever been run).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from assessment.config import AssessmentConfig
from assessment.orchestrator import run_assessment

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_DISCOVERY_MANIFEST = _REPO_ROOT / "output" / "discovery_manifest.json"


def _write_sample_discovery_manifest(path: Path) -> None:
    manifest = {
        "databases": [{"name": "SampleDb"}],
        "tables": [{"database": "SampleDb", "schema": "dbo", "name": "Orders", "column_count": 3, "index_count": 1,
                    "foreign_key_count": 0, "trigger_count": 0}],
        "views": [], "functions": [],
        "triggers": [],
        "stored_procedures": [{
            "database": "SampleDb", "schema": "dbo", "name": "usp_LoadOrders", "loc": 10,
            "referenced_tables": ["dbo.Orders"], "referenced_procs": [], "referenced_functions": [],
            "referenced_sequences": [], "compatibility_flags": [], "dynamic_sql_usage": False,
            "parse_status": "sqlglot", "unresolved_reason": None,
        }],
        "packages": [],
        "dependencies": [{"source_object": "dbo.usp_LoadOrders", "source_type": "stored_procedure",
                           "target_object": "dbo.Orders", "target_type": "table",
                           "relationship_type": "reads", "discovery_method": "sqlglot"}],
        "unsupported_objects": [],
        "data_quality_summary": [{"database": "SampleDb", "heap_tables": 1}],
        "security_principals": [{"scope": "server", "name": "sa", "member_of_roles": ["sysadmin"]}],
        "permissions": [], "linked_servers": [], "assemblies": [],
    }
    path.write_text(json.dumps(manifest))


def test_run_assessment_end_to_end_writes_all_outputs(tmp_path):
    input_path = tmp_path / "discovery_manifest.json"
    output_dir = tmp_path / "output_assessment"
    _write_sample_discovery_manifest(input_path)

    config = AssessmentConfig(input_manifest_path=str(input_path), output_dir=str(output_dir))
    manifest = run_assessment(config)

    assert manifest.database == "SampleDb"
    assert len(manifest.object_complexity) == 2  # 1 table + 1 proc
    assert len(manifest.migration_waves) == 2  # Orders (wave 0), then usp_LoadOrders (wave 1)
    assert len(manifest.data_readiness) == 1
    assert len(manifest.security_notes) >= 1

    for filename in (
        "assessment_manifest.json", "assessment_rollup.csv", "risk_register.csv",
        "object_complexity.csv", "migration_waves.csv", "assessment_report.md", "assessment_run.log",
    ):
        assert (output_dir / filename).exists(), f"missing {filename}"


def test_run_assessment_raises_informative_error_when_input_missing(tmp_path):
    config = AssessmentConfig(input_manifest_path=str(tmp_path / "does_not_exist.json"), output_dir=str(tmp_path / "out"))
    with pytest.raises(FileNotFoundError, match="Discovery manifest not found"):
        run_assessment(config)


def test_multi_database_manifest_adds_a_warning(tmp_path):
    input_path = tmp_path / "discovery_manifest.json"
    input_path.write_text(json.dumps({
        "databases": [{"name": "Db1"}, {"name": "Db2"}],
        "tables": [], "views": [], "functions": [], "triggers": [], "stored_procedures": [],
        "packages": [], "dependencies": [], "unsupported_objects": [], "data_quality_summary": [],
        "security_principals": [], "permissions": [], "linked_servers": [], "assemblies": [],
    }))
    config = AssessmentConfig(input_manifest_path=str(input_path), output_dir=str(tmp_path / "out"))
    manifest = run_assessment(config)
    assert any("more than one database" in w for w in manifest.warnings)


@pytest.mark.skipif(not _REAL_DISCOVERY_MANIFEST.exists(), reason="requires a real Discovery run's ./output/discovery_manifest.json")
def test_run_assessment_against_real_sqlglot_discovery_output(tmp_path):
    config = AssessmentConfig(input_manifest_path=str(_REAL_DISCOVERY_MANIFEST), output_dir=str(tmp_path / "output_assessment"))
    manifest = run_assessment(config)

    assert manifest.database
    assert len(manifest.object_complexity) > 0
    # The two AdventureWorks2022 multi-event triggers (Sales.iduSalesOrderDetail,
    # Person.iuPerson) must be merged, not scored 2-3x -- see complexity_scorer.py.
    trigger_names = [oc.name for oc in manifest.object_complexity if oc.object_type == "trigger"]
    assert len(trigger_names) == len(set(trigger_names)), "duplicate trigger rows were not merged"
    assert manifest.summary is not None
    assert manifest.summary.total_estimated_hours > 0
