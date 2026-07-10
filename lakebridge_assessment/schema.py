"""
Output contract for the Lakebridge Assessment phase. Deliberately its own
set of dataclasses -- independent of assessment/schema.py, same field
names/shapes retyped rather than imported -- so the two phases never share
code (same convention lakebridge_discovery/schema.py already follows
relative to autovista/schema.py) while still producing directly
comparable output_assessment/ vs output_lakebridge_assessment/ reports.

Reads lakebridge_discovery's manifest (./output_lakebridge/lakebridge_manifest.json)
as plain dicts, same "JSON contract, not a Python import" reasoning as
assessment/schema.py's own module docstring.

Key difference from assessment/schema.py's ObjectComplexity: complexity_tier
here comes directly from Lakebridge Analyzer's own `complexity` field on
each LakebridgeObjectRef (per the user's own choice -- this is meant to be
genuinely "Lakebridge's assessment," not our heuristic re-applied to their
data). complexity_score is therefore NOT an independently computed metric
-- it's derived from the tier purely to rank/sort the "top riskiest
objects" list, and is documented as such wherever it's used.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

ComplexityTier = Literal["Low", "Medium", "High", "Critical"]
Severity = Literal["Low", "Medium", "High", "Critical"]


@dataclass
class ObjectComplexity:
    object_type: str
    name: str
    complexity_tier: ComplexityTier = "Low"
    # Derived from complexity_tier for sorting only -- see module docstring.
    complexity_score: float = 0.0
    estimated_hours: float = 0.0
    fan_in: int = 0
    fan_out: int = 0
    compatibility_flags: list[str] = field(default_factory=list)
    source_tech: str = ""
    notes: str | None = None


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
class AssessmentSummary:
    database: str
    total_objects_scored: int = 0
    objects_without_native_complexity: int = 0
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
    complexity_source: str = "lakebridge_native"
    mapping_verified: bool = False
    mapping_notes: str = ""
    object_complexity: list[ObjectComplexity] = field(default_factory=list)
    risk_register: list[RiskFinding] = field(default_factory=list)
    migration_waves: list[MigrationWave] = field(default_factory=list)
    data_readiness: list[DataReadinessFinding] = field(default_factory=list)
    security_notes: list[SecurityNote] = field(default_factory=list)
    summary: AssessmentSummary | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
