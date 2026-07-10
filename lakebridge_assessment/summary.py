"""Rolls up object_complexity/risk_register/migration_waves into one
AssessmentSummary -- independent reimplementation of assessment/summary.py
(not imported -- see schema.py's module docstring)."""
from __future__ import annotations

from collections import Counter

from lakebridge_assessment.config import AssessmentConfig
from lakebridge_assessment.schema import AssessmentSummary, MigrationWave, ObjectComplexity, RiskFinding


def build_summary(
    database: str,
    object_complexity: list[ObjectComplexity],
    objects_without_native_complexity: int,
    risk_register: list[RiskFinding],
    migration_waves: list[MigrationWave],
    config: AssessmentConfig,
) -> AssessmentSummary:
    tier_counts = Counter(oc.complexity_tier for oc in object_complexity)
    severity_counts = Counter(r.severity for r in risk_register)
    category_counts = Counter(r.category for r in risk_register)

    riskiest = sorted(object_complexity, key=lambda oc: oc.complexity_score, reverse=True)
    top_riskiest = [f"{oc.object_type}:{oc.name} (tier={oc.complexity_tier})"
                    for oc in riskiest[:config.top_riskiest_object_count]]

    return AssessmentSummary(
        database=database,
        total_objects_scored=len(object_complexity),
        objects_without_native_complexity=objects_without_native_complexity,
        complexity_tier_counts=dict(tier_counts),
        total_estimated_hours=round(sum(oc.estimated_hours for oc in object_complexity), 2),
        risk_counts_by_severity=dict(severity_counts),
        risk_counts_by_category=dict(sorted(category_counts.items())),
        top_riskiest_objects=top_riskiest,
        total_migration_waves=len(migration_waves),
        waves_with_circular_dependencies=sum(1 for w in migration_waves if w.has_circular_dependency),
        effort_rubric_hours=config.effort_rubric.as_dict(),
    )
