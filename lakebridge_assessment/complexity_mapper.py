"""
Normalizes Lakebridge Analyzer's own native per-object complexity rating
(LakebridgeObjectRef.complexity, e.g. "LOW"/"MEDIUM" -- see
lakebridge_discovery/report_parser.py's _COMPLEXITY_KEYS) into this
package's ObjectComplexity contract.

Deliberately NOT a scoring formula: per the user's own choice, this module
trusts Lakebridge's own complexity judgment as-is rather than recomputing
one from LOC/reference-breadth/compatibility-flags the way
assessment/complexity_scorer.py does for the sqlglot engine. The only
things computed here are: (1) tier -> hours via the effort rubric (so the
two phases' hour totals are comparable), and (2) a sort-only
complexity_score derived from the tier + dependency fan-in/out (see
schema.py's ObjectComplexity docstring -- never treat this score as an
independently meaningful metric).

Objects with no native complexity value at all (None/blank -- e.g.
schemas, synonyms, and CLR assemblies in this project's own Analyzer
output) are skipped entirely rather than guessed at; see
AssessmentSummary.objects_without_native_complexity for how many were
skipped and why.
"""
from __future__ import annotations

from lakebridge_assessment.config import AssessmentConfig
from lakebridge_assessment.dependency_index import build_dependency_index
from lakebridge_assessment.schema import ObjectComplexity

_TIER_RANK = {"Low": 1, "Medium": 2, "High": 3, "Critical": 4}
_NORMALIZE = {
    "LOW": "Low", "MEDIUM": "Medium", "HIGH": "High", "CRITICAL": "Critical",
    "LOW COMPLEXITY": "Low", "MEDIUM COMPLEXITY": "Medium", "HIGH COMPLEXITY": "High",
}

_SCORED_CATEGORIES = ("tables", "views", "stored_procedures", "functions", "triggers")


def _normalize_tier(raw: str) -> str | None:
    return _NORMALIZE.get(raw.strip().upper())


def build_object_complexity(manifest: dict, config: AssessmentConfig) -> tuple[list[ObjectComplexity], int]:
    """Returns (scored objects, count of objects skipped for lacking a
    usable native complexity value)."""
    dep_index = build_dependency_index(manifest.get("dependencies", []))
    results: list[ObjectComplexity] = []
    skipped = 0

    for object_type, field_name in (
        ("table", "tables"), ("view", "views"), ("stored_procedure", "stored_procedures"),
        ("function", "functions"), ("trigger", "triggers"),
    ):
        for obj in manifest.get(field_name, []):
            raw_complexity = obj.get("complexity")
            tier = _normalize_tier(raw_complexity) if raw_complexity else None
            if tier is None:
                skipped += 1
                continue

            name = obj.get("name", "(unnamed)")
            fan_in = dep_index.fan_in(name)
            fan_out = dep_index.fan_out(name)
            score = _TIER_RANK[tier] * 10 + 0.1 * (fan_in + fan_out)

            results.append(ObjectComplexity(
                object_type=object_type, name=name, complexity_tier=tier,
                complexity_score=round(score, 2), estimated_hours=config.effort_rubric.hours_for_tier(tier),
                fan_in=fan_in, fan_out=fan_out,
                compatibility_flags=obj.get("compatibility_flags") or [],
                source_tech=obj.get("source_tech", ""), notes=obj.get("notes"),
            ))

    return results, skipped
