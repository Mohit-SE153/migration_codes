"""
Configuration for the Assessment phase. Same "never hardcode, everything
overridable via environment variable" convention as autovista/config.py,
reusing that module's .env loader rather than duplicating it (both phases
live in the same repo/.env, so there's exactly one place secrets/overrides
are read from).

The effort-hours-per-tier rubric (ASSESSMENT_HOURS_*) is a stated
assumption, not a measured fact -- see schema.py's AssessmentSummary
docstring and complexity_scorer.py. Override it here once real team
velocity data exists instead of editing the scoring code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from autovista.config import _load_dotenv_if_present

_load_dotenv_if_present()


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class EffortRubric:
    """Hours assumed for one object at each complexity tier. Purely a
    planning heuristic -- multiply by object counts to get a rough
    estate-level effort estimate, not a committed quote. Defaults are a
    reasonable starting point for hand-remediation of T-SQL/SSIS objects
    during a SQL Server -> Databricks migration; replace with your team's
    own measured velocity as soon as you have a few objects' actuals."""

    low_hours: float = 0.5
    medium_hours: float = 2.0
    high_hours: float = 6.0
    critical_hours: float = 8.0

    def hours_for_tier(self, tier: str) -> float:
        return {
            "Low": self.low_hours,
            "Medium": self.medium_hours,
            "High": self.high_hours,
            "Critical": self.critical_hours,
        }[tier]

    def as_dict(self) -> dict:
        return {
            "Low": self.low_hours, "Medium": self.medium_hours,
            "High": self.high_hours, "Critical": self.critical_hours,
        }


@dataclass(frozen=True)
class ComplexityThresholds:
    """Score cutoffs (exclusive upper bound) separating complexity tiers --
    see complexity_scorer.py for how the raw score is computed. Assumed,
    tunable; not derived from any external benchmark."""

    low_max: float = 4.0
    medium_max: float = 10.0
    high_max: float = 20.0

    def tier_for_score(self, score: float) -> str:
        if score < self.low_max:
            return "Low"
        if score < self.medium_max:
            return "Medium"
        if score < self.high_max:
            return "High"
        return "Critical"


@dataclass(frozen=True)
class AssessmentConfig:
    input_manifest_path: str = "./output/discovery_manifest.json"
    output_dir: str = "./output_assessment/"
    top_riskiest_object_count: int = 10
    effort_rubric: EffortRubric = field(default_factory=EffortRubric)
    thresholds: ComplexityThresholds = field(default_factory=ComplexityThresholds)


def load_config() -> AssessmentConfig:
    return AssessmentConfig(
        input_manifest_path=os.environ.get("ASSESSMENT_INPUT_MANIFEST", "./output/discovery_manifest.json"),
        output_dir=os.environ.get("ASSESSMENT_OUTPUT_DIR", "./output_assessment/"),
        top_riskiest_object_count=int(os.environ.get("ASSESSMENT_TOP_RISKIEST_COUNT", "10")),
        effort_rubric=EffortRubric(
            low_hours=_float_env("ASSESSMENT_HOURS_LOW", 2.0),
            medium_hours=_float_env("ASSESSMENT_HOURS_MEDIUM", 6.0),
            high_hours=_float_env("ASSESSMENT_HOURS_HIGH", 16.0),
            critical_hours=_float_env("ASSESSMENT_HOURS_CRITICAL", 32.0),
        ),
        thresholds=ComplexityThresholds(
            low_max=_float_env("ASSESSMENT_THRESHOLD_LOW_MAX", 4.0),
            medium_max=_float_env("ASSESSMENT_THRESHOLD_MEDIUM_MAX", 10.0),
            high_max=_float_env("ASSESSMENT_THRESHOLD_HIGH_MAX", 20.0),
        ),
    )
