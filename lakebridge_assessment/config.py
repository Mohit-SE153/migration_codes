"""
Configuration for the Lakebridge Assessment phase. Independent of
assessment/config.py (not shared code, same convention as the rest of
this package) -- but the default effort-hours-per-tier rubric values match
assessment/config.py's defaults on purpose, so a "Low" object costs the
same assumed hours in both reports and the two totals are comparable.
Override independently via LAKEBRIDGE_ASSESSMENT_* if you want to test the
two rubrics diverging.
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
    low_hours: float = 2.0
    medium_hours: float = 6.0
    high_hours: float = 16.0
    critical_hours: float = 32.0

    def hours_for_tier(self, tier: str) -> float:
        return {
            "Low": self.low_hours, "Medium": self.medium_hours,
            "High": self.high_hours, "Critical": self.critical_hours,
        }[tier]

    def as_dict(self) -> dict:
        return {
            "Low": self.low_hours, "Medium": self.medium_hours,
            "High": self.high_hours, "Critical": self.critical_hours,
        }


@dataclass(frozen=True)
class AssessmentConfig:
    input_manifest_path: str = "./output_lakebridge/lakebridge_manifest.json"
    output_dir: str = "./output_lakebridge_assessment/"
    top_riskiest_object_count: int = 10
    effort_rubric: EffortRubric = field(default_factory=EffortRubric)


def load_config() -> AssessmentConfig:
    return AssessmentConfig(
        input_manifest_path=os.environ.get("LAKEBRIDGE_ASSESSMENT_INPUT_MANIFEST", "./output_lakebridge/lakebridge_manifest.json"),
        output_dir=os.environ.get("LAKEBRIDGE_ASSESSMENT_OUTPUT_DIR", "./output_lakebridge_assessment/"),
        top_riskiest_object_count=int(os.environ.get("LAKEBRIDGE_ASSESSMENT_TOP_RISKIEST_COUNT", "10")),
        effort_rubric=EffortRubric(
            low_hours=_float_env("LAKEBRIDGE_ASSESSMENT_HOURS_LOW", 2.0),
            medium_hours=_float_env("LAKEBRIDGE_ASSESSMENT_HOURS_MEDIUM", 6.0),
            high_hours=_float_env("LAKEBRIDGE_ASSESSMENT_HOURS_HIGH", 16.0),
            critical_hours=_float_env("LAKEBRIDGE_ASSESSMENT_HOURS_CRITICAL", 32.0),
        ),
    )
