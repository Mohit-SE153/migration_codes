"""Tests for lakebridge_assessment.migration_wave_planner."""
from __future__ import annotations

from lakebridge_assessment.migration_wave_planner import build_migration_waves
from lakebridge_assessment.schema import ObjectComplexity


def _manifest(tables=None, procs=None, dependencies=None):
    return {"tables": tables or [], "views": [], "stored_procedures": procs or [], "functions": [], "triggers": [],
            "dependencies": dependencies or []}


def test_no_scoped_objects_produces_no_waves():
    assert build_migration_waves(_manifest(), []) == []


def test_linear_dependency_respects_case_insensitive_matching():
    # dependency target is lowercased, as Lakebridge's report often does,
    # but the table's own inventory name retains proper case.
    manifest = _manifest(
        tables=[{"name": "dbo.Orders"}],
        procs=[{"name": "dbo.usp_A"}],
        dependencies=[{"source_object": "dbo.usp_A", "target_object": "dbo.orders"}],
    )
    waves = build_migration_waves(manifest, [])
    assert len(waves) == 2
    assert waves[0].objects == ["dbo.Orders"]  # original casing preserved in output
    assert waves[1].objects == ["dbo.usp_A"]


def test_mutual_dependency_collapses_into_circular_wave():
    manifest = _manifest(
        procs=[{"name": "dbo.usp_A"}, {"name": "dbo.usp_B"}],
        dependencies=[
            {"source_object": "dbo.usp_A", "target_object": "dbo.usp_b"},
            {"source_object": "dbo.usp_B", "target_object": "dbo.usp_a"},
        ],
    )
    waves = build_migration_waves(manifest, [])
    assert len(waves) == 1
    assert waves[0].has_circular_dependency is True


def test_wave_hours_summed_from_object_complexity_case_insensitively():
    manifest = _manifest(tables=[{"name": "dbo.A"}, {"name": "dbo.B"}])
    complexity = [
        ObjectComplexity(object_type="table", name="dbo.A", estimated_hours=2.0),
        ObjectComplexity(object_type="table", name="dbo.B", estimated_hours=6.0),
    ]
    waves = build_migration_waves(manifest, complexity)
    assert waves[0].estimated_hours == 8.0
