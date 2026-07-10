"""
Configuration for the LLM Assessment phase. Fully self-contained --
deliberately NOT importing assessment.config or autovista.config, so this
package keeps working even if assessment/, lakebridge_assessment/, and
autovista/ are all deleted later. Only real remaining dependency is the
Discovery manifest JSON *file* already sitting on disk, not any package
that produced it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _load_dotenv_if_present() -> None:
    """Own copy of the same .env loader autovista/config.py uses --
    duplicated rather than imported so this package has zero dependency
    on autovista/ (see module docstring)."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv_if_present()


@dataclass(frozen=True)
class EffortRubric:
    """Hours assumed for one object at each complexity tier. Purely a
    planning heuristic -- multiply by object counts to get a rough
    estate-level effort estimate, not a committed quote. Own copy of
    assessment/config.py's EffortRubric shape (not imported, see module
    docstring) -- defaults intentionally match this project's latest
    tuning of that rubric (see assessment/config.py) so the two hour
    totals stay comparable unless you deliberately diverge them."""

    low_hours: float = 0.5
    medium_hours: float = 2.0
    high_hours: float = 6.0
    critical_hours: float = 8.0

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
class LlmAssessmentConfig:
    input_manifest_path: str = "./output/discovery_manifest.json"
    output_dir: str = "./output_llm_assessment/"
    api_key: str | None = None
    model: str = "claude-haiku-4-5-20251001"
    # Hard cap on LLM calls per run -- cost/latency scale with object
    # count (see llm_client.py). Objects beyond the cap are marked
    # skipped (not silently dropped, not guessed) -- see complexity_scorer.py.
    max_objects_per_run: int = 200
    top_riskiest_object_count: int = 10
    effort_rubric: EffortRubric = field(default_factory=EffortRubric)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


def load_config() -> LlmAssessmentConfig:
    return LlmAssessmentConfig(
        input_manifest_path=os.environ.get("LLM_ASSESSMENT_INPUT_MANIFEST", "./output/discovery_manifest.json"),
        output_dir=os.environ.get("LLM_ASSESSMENT_OUTPUT_DIR", "./output_llm_assessment/"),
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        model=os.environ.get("LLM_ASSESSMENT_MODEL", "claude-haiku-4-5-20251001"),
        max_objects_per_run=int(os.environ.get("LLM_ASSESSMENT_MAX_OBJECTS_PER_RUN", "200")),
        top_riskiest_object_count=int(os.environ.get("LLM_ASSESSMENT_TOP_RISKIEST_COUNT", "10")),
        effort_rubric=EffortRubric(
            low_hours=float(os.environ.get("LLM_ASSESSMENT_HOURS_LOW", "0.5")),
            medium_hours=float(os.environ.get("LLM_ASSESSMENT_HOURS_MEDIUM", "2.0")),
            high_hours=float(os.environ.get("LLM_ASSESSMENT_HOURS_HIGH", "6.0")),
            critical_hours=float(os.environ.get("LLM_ASSESSMENT_HOURS_CRITICAL", "8.0")),
        ),
    )
