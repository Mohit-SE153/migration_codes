"""
Tests for llm_assessment.orchestrator. Always injects a fake LlmClient --
this test suite must never make a real Anthropic API call (cost/
determinism). The real live run against ./output/discovery_manifest.json
is a one-off, manually-triggered thing, not something pytest should do
automatically on every run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_assessment.config import LlmAssessmentConfig
from llm_assessment.orchestrator import run_assessment


class _FakeLlmClient:
    def complete_json(self, system_prompt: str, user_text: str) -> dict:
        return {"tier": "Medium", "confidence": "high", "rationale": "fake rationale for testing"}


def _write_sample_manifest(path: Path) -> None:
    manifest = {
        "databases": [{"name": "SampleDb"}],
        "tables": [{"database": "SampleDb", "schema": "dbo", "name": "Orders", "column_count": 3}],
        "views": [], "functions": [], "triggers": [],
        "stored_procedures": [{
            "database": "SampleDb", "schema": "dbo", "name": "usp_LoadOrders", "loc": 10,
            "referenced_tables": ["dbo.Orders"], "referenced_procs": [], "referenced_functions": [],
            "referenced_sequences": [], "compatibility_flags": [], "parse_status": "sqlglot",
        }],
        "packages": [],
        "dependencies": [{"source_object": "dbo.usp_LoadOrders", "source_type": "stored_procedure",
                           "target_object": "dbo.Orders", "target_type": "table",
                           "relationship_type": "reads", "discovery_method": "sqlglot"}],
        "unsupported_objects": [], "data_quality_summary": [], "security_principals": [],
        "permissions": [], "linked_servers": [], "assemblies": [],
    }
    path.write_text(json.dumps(manifest))


def test_run_assessment_with_fake_client_writes_outputs_and_scores_objects(tmp_path):
    input_path = tmp_path / "discovery_manifest.json"
    output_dir = tmp_path / "output_llm_assessment"
    _write_sample_manifest(input_path)

    config = LlmAssessmentConfig(input_manifest_path=str(input_path), output_dir=str(output_dir))
    manifest = run_assessment(config, client=_FakeLlmClient())

    assert manifest.database == "SampleDb"
    assert len(manifest.object_complexity) == 2
    assert all(oc.complexity_tier == "Medium" for oc in manifest.object_complexity)
    assert any("LLM" in w for w in manifest.warnings)

    for filename in (
        "llm_assessment_manifest.json", "llm_assessment_rollup.csv", "risk_register.csv",
        "object_complexity.csv", "migration_waves.csv", "llm_assessment_report.md", "llm_assessment_run.log",
    ):
        assert (output_dir / filename).exists(), f"missing {filename}"


def test_run_assessment_without_client_leaves_everything_unscored_but_still_writes_output(tmp_path):
    input_path = tmp_path / "discovery_manifest.json"
    output_dir = tmp_path / "output_llm_assessment"
    _write_sample_manifest(input_path)

    config = LlmAssessmentConfig(input_manifest_path=str(input_path), output_dir=str(output_dir), api_key=None)
    manifest = run_assessment(config, client=None)

    assert manifest.object_complexity == []
    assert any("skipped_no_client" in w or "no ANTHROPIC_API_KEY" in w for w in manifest.warnings)
    assert (output_dir / "llm_assessment_manifest.json").exists()


def test_run_assessment_raises_informative_error_when_input_missing(tmp_path):
    config = LlmAssessmentConfig(input_manifest_path=str(tmp_path / "missing.json"), output_dir=str(tmp_path / "out"))
    with pytest.raises(FileNotFoundError, match="Discovery manifest not found"):
        run_assessment(config, client=_FakeLlmClient())


def test_build_llm_client_returns_none_without_api_key():
    from llm_assessment.orchestrator import build_llm_client
    config = LlmAssessmentConfig(api_key=None)
    assert build_llm_client(config) is None


def test_build_llm_client_returns_client_with_api_key():
    from llm_assessment.orchestrator import build_llm_client
    config = LlmAssessmentConfig(api_key="sk-fake-key", model="claude-haiku-4-5-20251001")
    client = build_llm_client(config)
    assert client is not None
    assert client.model == "claude-haiku-4-5-20251001"
