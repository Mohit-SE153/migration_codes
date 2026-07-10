"""Tests for assessment.migration_wave_planner."""
from __future__ import annotations

from assessment.migration_wave_planner import build_migration_waves
from assessment.schema import ObjectComplexity


def _manifest(tables=None, procs=None, views=None, dependencies=None):
    return {
        "tables": tables or [], "views": views or [], "functions": [], "triggers": [],
        "stored_procedures": procs or [], "dependencies": dependencies or [],
    }


def test_no_scoped_objects_produces_no_waves():
    assert build_migration_waves(_manifest(), []) == []


def test_independent_objects_with_no_dependencies_all_land_in_wave_zero():
    manifest = _manifest(tables=[
        {"schema": "dbo", "name": "A"}, {"schema": "dbo", "name": "B"},
    ])
    waves = build_migration_waves(manifest, [])
    assert len(waves) == 1
    assert waves[0].wave_number == 0
    assert set(waves[0].objects) == {"dbo.A", "dbo.B"}
    assert waves[0].has_circular_dependency is False


def test_linear_dependency_chain_produces_ordered_waves():
    # usp_A reads dbo.Orders -- Orders must be migrated first.
    manifest = _manifest(
        tables=[{"schema": "dbo", "name": "Orders"}],
        procs=[{"schema": "dbo", "name": "usp_A"}],
        dependencies=[{"source_object": "dbo.usp_A", "target_object": "dbo.Orders"}],
    )
    waves = build_migration_waves(manifest, [])
    assert len(waves) == 2
    assert waves[0].objects == ["dbo.Orders"]
    assert waves[1].objects == ["dbo.usp_A"]


def test_mutual_dependency_collapses_into_one_circular_wave():
    manifest = _manifest(
        procs=[{"schema": "dbo", "name": "usp_A"}, {"schema": "dbo", "name": "usp_B"}],
        dependencies=[
            {"source_object": "dbo.usp_A", "target_object": "dbo.usp_B"},
            {"source_object": "dbo.usp_B", "target_object": "dbo.usp_A"},
        ],
    )
    waves = build_migration_waves(manifest, [])
    assert len(waves) == 1
    assert waves[0].has_circular_dependency is True
    assert set(waves[0].objects) == {"dbo.usp_A", "dbo.usp_B"}


def test_edges_to_objects_outside_scope_are_ignored():
    # target_object "dbo.SomeSequence" isn't a table/view/proc/function/trigger
    # in this manifest, so it shouldn't block usp_A from landing in wave 0.
    manifest = _manifest(
        procs=[{"schema": "dbo", "name": "usp_A"}],
        dependencies=[{"source_object": "dbo.usp_A", "target_object": "dbo.SomeSequence"}],
    )
    waves = build_migration_waves(manifest, [])
    assert len(waves) == 1
    assert waves[0].wave_number == 0
    assert waves[0].objects == ["dbo.usp_A"]


def test_wave_estimated_hours_sum_from_object_complexity():
    manifest = _manifest(tables=[{"schema": "dbo", "name": "A"}, {"schema": "dbo", "name": "B"}])
    complexity = [
        ObjectComplexity(object_type="table", name="dbo.A", database="db", estimated_hours=2.0),
        ObjectComplexity(object_type="table", name="dbo.B", database="db", estimated_hours=6.0),
    ]
    waves = build_migration_waves(manifest, complexity)
    assert waves[0].estimated_hours == 8.0
