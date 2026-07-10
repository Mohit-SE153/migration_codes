"""
Output contract for the LLM Assessment phase. Fully self-contained --
deliberately NOT importing assessment/schema.py, so this package keeps
working even if assessment/ and lakebridge_assessment/ are deleted later.
Same field names/shapes as assessment/schema.py (retyped, not imported)
purely so the three tools' output stays comparable by eye; this is the
same "independent copy, comparable shape" convention lakebridge_discovery/
already uses relative to autovista/.

Reads Discovery's manifest (./output/discovery_manifest.json) as plain
dicts -- never imports autovista's internals either.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

ComplexityTier = Literal["Low", "Medium", "High", "Critical"]
Severity = Literal["Low", "Medium", "High", "Critical"]


@dataclass
class ObjectComplexity:
    """complexity_tier here is an LLM judgment call (see complexity_scorer.py),
    never independently computed -- complexity_score is derived from the
    tier purely for sorting the "top riskiest objects" list."""

    object_type: str
    name: str
    database: str
    loc: int = 0
    compatibility_flags: list[str] = field(default_factory=list)
    dynamic_sql_usage: bool = False
    parse_status: str | None = None
    unresolved_reason: str | None = None
    fan_in: int = 0
    fan_out: int = 0
    complexity_score: float = 0.0
    complexity_tier: ComplexityTier = "Low"
    estimated_hours: float = 0.0
    scoring_reasons: list[str] = field(default_factory=list)


@dataclass
class RiskFinding:
    object_type: str
    name: str
    category: str
    severity: Severity
    description: str
    remediation: str | None = None
    needs_human_review: bool = True


@dataclass
class MigrationWave:
    wave_number: int
    objects: list[str] = field(default_factory=list)
    object_count: int = 0
    estimated_hours: float = 0.0
    rationale: str = ""
    has_circular_dependency: bool = False


@dataclass
class DataReadinessFinding:
    category: str
    count: int
    severity: Severity
    description: str
    recommendation: str
    sample_objects: list[str] = field(default_factory=list)


@dataclass
class SecurityNote:
    category: str
    count: int
    severity: Severity
    description: str
    recommendation: str


@dataclass
class InfraSizingRecommendation:
    """One Databricks infrastructure-sizing signal derived from Discovery's
    database/table size metadata (see infra_sizing.py) -- deterministic,
    threshold-based, grounded in Databricks' own published sizing guidance
    (SQL warehouse t-shirt sizes, the "don't partition under 1TB, use
    Liquid Clustering instead" recommendation), not an LLM judgment call.
    A capacity-planning starting point, not a committed infra spec --
    always re-validate against actual query/concurrency patterns once you
    have them, which this metadata-only signal can't see."""

    category: str
    current_metric: str
    recommendation: str
    rationale: str


@dataclass
class AssessmentSummary:
    database: str
    total_objects_scored: int = 0
    complexity_tier_counts: dict = field(default_factory=dict)
    total_estimated_hours: float = 0.0
    risk_counts_by_severity: dict = field(default_factory=dict)
    risk_counts_by_category: dict = field(default_factory=dict)
    top_riskiest_objects: list[str] = field(default_factory=list)
    total_migration_waves: int = 0
    waves_with_circular_dependencies: int = 0
    effort_rubric_hours: dict = field(default_factory=dict)


@dataclass
class AssessmentManifest:
    generated_at: str = ""
    source_manifest_path: str = ""
    database: str = ""
    object_complexity: list[ObjectComplexity] = field(default_factory=list)
    risk_register: list[RiskFinding] = field(default_factory=list)
    migration_waves: list[MigrationWave] = field(default_factory=list)
    data_readiness: list[DataReadinessFinding] = field(default_factory=list)
    security_notes: list[SecurityNote] = field(default_factory=list)
    infra_sizing: list[InfraSizingRecommendation] = field(default_factory=list)
    summary: AssessmentSummary | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
