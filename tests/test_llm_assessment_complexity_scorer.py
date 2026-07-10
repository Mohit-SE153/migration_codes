"""
Tests for llm_assessment.complexity_scorer -- all use a fake in-memory
LlmClient (never a real Anthropic API call) so the test suite stays free,
fast, and deterministic.
"""
from __future__ import annotations

from llm_assessment.complexity_scorer import build_object_complexity
from llm_assessment.config import LlmAssessmentConfig


class _FakeLlmClient:
    def __init__(self, response: dict | None = None, raise_error: bool = False):
        self.response = response or {"tier": "Medium", "confidence": "high", "rationale": "test rationale"}
        self.raise_error = raise_error
        self.calls: list[str] = []

    def complete_json(self, system_prompt: str, user_text: str) -> dict:
        self.calls.append(user_text)
        if self.raise_error:
            raise RuntimeError("simulated network failure")
        return self.response


def _config(**overrides) -> LlmAssessmentConfig:
    return LlmAssessmentConfig(**overrides)


def test_no_client_leaves_every_object_unscored():
    manifest = {"tables": [{"schema": "dbo", "name": "Orders"}], "views": [], "stored_procedures": [],
                "functions": [], "triggers": [], "dependencies": []}
    results, stats = build_object_complexity(manifest, _config(), client=None)
    assert results == []
    assert stats["skipped_no_client"] == 1
    assert stats["attempted"] == 0


def test_successful_call_produces_scored_object_with_llm_tier():
    manifest = {"tables": [{"schema": "dbo", "name": "Orders", "database": "db"}], "views": [], "stored_procedures": [],
                "functions": [], "triggers": [], "dependencies": []}
    client = _FakeLlmClient(response={"tier": "High", "confidence": "medium", "rationale": "many indexes"})
    results, stats = build_object_complexity(manifest, _config(), client=client)
    assert len(results) == 1
    assert results[0].complexity_tier == "High"
    assert "many indexes" in results[0].scoring_reasons[0]
    assert stats["succeeded"] == 1
    assert stats["attempted"] == 1


def test_unrecognized_tier_from_model_counts_as_failed_not_guessed():
    manifest = {"tables": [{"schema": "dbo", "name": "Orders"}], "views": [], "stored_procedures": [],
                "functions": [], "triggers": [], "dependencies": []}
    client = _FakeLlmClient(response={"tier": "Extreme", "confidence": "high", "rationale": "??"})
    results, stats = build_object_complexity(manifest, _config(), client=client)
    assert results == []
    assert stats["failed"] == 1


def test_client_exception_does_not_crash_the_run_and_is_counted_failed():
    manifest = {"tables": [{"schema": "dbo", "name": "A"}, {"schema": "dbo", "name": "B"}], "views": [],
                "stored_procedures": [], "functions": [], "triggers": [], "dependencies": []}
    client = _FakeLlmClient(raise_error=True)
    results, stats = build_object_complexity(manifest, _config(), client=client)
    assert results == []
    assert stats["failed"] == 2
    assert stats["attempted"] == 2


def test_max_objects_per_run_cap_is_respected():
    manifest = {"tables": [{"schema": "dbo", "name": f"T{i}"} for i in range(5)], "views": [],
                "stored_procedures": [], "functions": [], "triggers": [], "dependencies": []}
    client = _FakeLlmClient()
    results, stats = build_object_complexity(manifest, _config(max_objects_per_run=2), client=client)
    assert stats["attempted"] == 2
    assert stats["skipped_capped"] == 3
    assert len(results) == 2


def test_estimated_hours_uses_configured_rubric():
    manifest = {"tables": [{"schema": "dbo", "name": "Orders"}], "views": [], "stored_procedures": [],
                "functions": [], "triggers": [], "dependencies": []}
    client = _FakeLlmClient(response={"tier": "Critical", "confidence": "high", "rationale": "x"})
    config = _config()
    results, _ = build_object_complexity(manifest, config, client=client)
    assert results[0].estimated_hours == config.effort_rubric.critical_hours


def test_duplicate_trigger_rows_are_merged_before_scoring():
    manifest = {"tables": [], "views": [], "stored_procedures": [], "functions": [], "dependencies": [],
                "triggers": [
                    {"schema": "Sales", "name": "Trg", "event": "INSERT"},
                    {"schema": "Sales", "name": "Trg", "event": "UPDATE"},
                ]}
    client = _FakeLlmClient()
    results, stats = build_object_complexity(manifest, _config(), client=client)
    assert len(results) == 1
    assert stats["attempted"] == 1
