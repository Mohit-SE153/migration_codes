"""Tests for lakebridge_assessment.complexity_mapper."""
from __future__ import annotations

from lakebridge_assessment.complexity_mapper import build_object_complexity
from lakebridge_assessment.config import AssessmentConfig


def _config() -> AssessmentConfig:
    return AssessmentConfig()


def test_native_complexity_used_verbatim_after_normalization():
    manifest = {"tables": [{"name": "dbo.Orders", "complexity": "LOW"}], "views": [], "stored_procedures": [],
                "functions": [], "triggers": [], "dependencies": []}
    results, skipped = build_object_complexity(manifest, _config())
    assert skipped == 0
    assert len(results) == 1
    assert results[0].complexity_tier == "Low"
    assert results[0].estimated_hours == _config().effort_rubric.low_hours


def test_object_with_no_complexity_value_is_skipped_not_guessed():
    manifest = {"tables": [{"name": "dbo.NoRating"}], "views": [], "stored_procedures": [],
                "functions": [], "triggers": [], "dependencies": []}
    results, skipped = build_object_complexity(manifest, _config())
    assert results == []
    assert skipped == 1


def test_unrecognized_complexity_value_is_skipped():
    manifest = {"tables": [{"name": "dbo.Weird", "complexity": "SUPER_DUPER_HARD"}], "views": [],
                "stored_procedures": [], "functions": [], "triggers": [], "dependencies": []}
    results, skipped = build_object_complexity(manifest, _config())
    assert results == []
    assert skipped == 1


def test_fan_in_fan_out_matched_case_insensitively():
    manifest = {
        "tables": [{"name": "dbo.Orders", "complexity": "LOW"}],
        "views": [], "functions": [], "triggers": [],
        "stored_procedures": [{"name": "dbo.usp_A", "complexity": "MEDIUM"}],
        "dependencies": [{"source_object": "dbo.usp_A", "target_object": "dbo.orders"}],  # lowercase target
    }
    results, _ = build_object_complexity(manifest, _config())
    table = next(r for r in results if r.object_type == "table")
    proc = next(r for r in results if r.object_type == "stored_procedure")
    assert table.fan_in == 1
    assert proc.fan_out == 1


def test_medium_ranks_higher_than_low_for_sorting():
    manifest = {
        "tables": [{"name": "dbo.A", "complexity": "LOW"}, {"name": "dbo.B", "complexity": "MEDIUM"}],
        "views": [], "stored_procedures": [], "functions": [], "triggers": [], "dependencies": [],
    }
    results, _ = build_object_complexity(manifest, _config())
    low = next(r for r in results if r.name == "dbo.A")
    medium = next(r for r in results if r.name == "dbo.B")
    assert medium.complexity_score > low.complexity_score
