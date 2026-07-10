"""
Output contract for the Assessment phase.

Assessment consumes Discovery's JSON manifest (autovista/schema.py's
DiscoveryManifest.to_dict() shape, e.g. ./output/discovery_manifest.json) as
plain dicts -- it never imports autovista's internal dataclasses. Discovery's
own schema.py docstring calls the JSON manifest "the single source of truth
... consumed by the downstream Assessment phase"; treating it as a JSON
contract (not a Python import) keeps the two phases independently runnable,
exactly like the sqlglot/Lakebridge engines stay independent of each other.

Only the sqlglot-engine manifest (./output/) is read by this build --
Lakebridge's manifest (./output_lakebridge/) is a structurally different
schema (see discovery_comparison/) and is out of scope here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

ComplexityTier = Literal["Low", "Medium", "High", "Critical"]
Severity = Literal["Low", "Medium", "High", "Critical"]


@dataclass
class ObjectComplexity:
    """Per-object migration-complexity score. Two independent scoring
    models feed into the same tier/hours scale (see complexity_scorer.py):
    a lineage/code model (stored procs, views, functions, triggers, SSIS
    embedded SQL) driven by LOC/reference-breadth/compatibility flags/parse
    health, and a schema model (tables) driven by DDL feature richness
    (temporal, CDC, memory-optimized, computed columns, ...). Both produce
    the same fields below so they can be rolled up together."""

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
    """One triageable migration risk. category is a short, stable code
    (e.g. "PARSE_UNRESOLVED", "COMPAT_LINKED_SERVER", "NO_PRIMARY_KEY") so
    findings can be grouped/counted without string-matching descriptions."""

    object_type: str
    name: str
    category: str
    severity: Severity
    description: str
    remediation: str | None = None
    needs_human_review: bool = True


@dataclass
class MigrationWave:
    """One batch of objects that can be migrated together, ordered so a
    later wave never depends on an object that hasn't been migrated yet
    (see migration_wave_planner.py). Objects inside a mutual/circular
    dependency collapse into a single wave together, flagged via
    has_circular_dependency, since no valid linear order exists for them."""

    wave_number: int
    objects: list[str] = field(default_factory=list)
    object_count: int = 0
    estimated_hours: float = 0.0
    rationale: str = ""
    has_circular_dependency: bool = False


@dataclass
class DataReadinessFinding:
    """One migration-readiness signal rolled up from Discovery's
    data_quality_summary (metadata-only, no new queries -- see
    data_readiness.py) into an assessed severity + recommendation."""

    category: str
    count: int
    severity: Severity
    description: str
    recommendation: str
    sample_objects: list[str] = field(default_factory=list)


@dataclass
class SecurityNote:
    """One security/permissions migration consideration, rolled up from
    Discovery's security_principals/permissions/linked_servers/assemblies
    (see security_review.py). Informational for planning Unity Catalog's
    principal/grant model -- never a security scan or a completeness
    guarantee over the source estate's actual security posture."""

    category: str
    count: int
    severity: Severity
    description: str
    recommendation: str


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
    # The assumed, editable hours-per-tier rubric actually used to compute
    # every ObjectComplexity.estimated_hours in this run -- surfaced here so
    # a reader isn't left guessing what "Medium = how many hours" meant for
    # this specific report (see config.py's ASSESSMENT_HOURS_* overrides).
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
    summary: AssessmentSummary | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
