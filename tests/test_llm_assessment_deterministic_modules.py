"""
Smoke tests for llm_assessment's independent-copy deterministic modules
(risk_register, migration_wave_planner, data_readiness, security_review,
output_writer). These mirror already-exhaustively-tested logic in
assessment/'s equivalents (see tests/test_assessment_*.py) -- these tests
exist to confirm the standalone copies under llm_assessment/ work
correctly on their own, not to re-litigate every edge case a second time.
"""
from __future__ import annotations

import json

from llm_assessment.data_readiness import build_data_readiness
from llm_assessment.migration_wave_planner import build_migration_waves
from llm_assessment.output_writer import write_manifest_json
from llm_assessment.risk_register import build_risk_register
from llm_assessment.schema import AssessmentManifest, AssessmentSummary, ObjectComplexity
from llm_assessment.security_review import build_security_notes


def test_risk_register_flags_unresolved_object():
    manifest = {"unsupported_objects": [{"object_type": "view", "name": "dbo.Bad", "parse_status": "unresolved", "reason": "boom"}]}
    findings = build_risk_register(manifest)
    assert len(findings) == 1
    assert findings[0].severity == "High"


def test_migration_wave_planner_orders_linear_dependency():
    manifest = {
        "tables": [{"schema": "dbo", "name": "Orders"}], "views": [], "stored_procedures": [{"schema": "dbo", "name": "usp_A"}],
        "functions": [], "triggers": [],
        "dependencies": [{"source_object": "dbo.usp_A", "target_object": "dbo.Orders"}],
    }
    waves = build_migration_waves(manifest, [])
    assert len(waves) == 2
    assert waves[0].objects == ["dbo.Orders"]
    assert waves[1].objects == ["dbo.usp_A"]


def test_data_readiness_flags_cdc_tables():
    manifest = {"data_quality_summary": [{"database": "db", "tables_with_cdc_enabled": 2}]}
    findings = build_data_readiness(manifest)
    assert len(findings) == 1
    assert findings[0].severity == "High"


def test_security_review_flags_sysadmin_login():
    manifest = {"security_principals": [{"scope": "server", "name": "sa", "member_of_roles": ["sysadmin"]}]}
    notes = build_security_notes(manifest)
    assert len(notes) == 1
    assert notes[0].severity == "High"


def test_output_writer_writes_full_manifest_with_infra_sizing(tmp_path):
    manifest = AssessmentManifest(
        database="TestDb",
        object_complexity=[ObjectComplexity(object_type="table", name="dbo.T", database="TestDb", complexity_tier="Low")],
        summary=AssessmentSummary(database="TestDb"),
    )
    path = write_manifest_json(manifest, str(tmp_path))
    with open(path) as f:
        data = json.load(f)
    assert "infra_sizing" in data
    assert data["object_complexity"][0]["name"] == "dbo.T"
