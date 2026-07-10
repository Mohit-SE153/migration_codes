"""
LLM Assessment orchestrator: reads the sqlglot Discovery engine's JSON
manifest (./output/discovery_manifest.json by default), scores every
table/view/stored-procedure/function/trigger's complexity tier via an
LLM call (see complexity_scorer.py for exactly what it's given and the
mandatory human-review contract), builds deterministic risk/wave/data-
readiness/security/infra-sizing sections, and writes a report.

Fully self-contained: every module this orchestrator wires together
(complexity_scorer, risk_register, migration_wave_planner, data_readiness,
security_review, infra_sizing, summary, output_writer, schema) lives
under llm_assessment/ itself -- none are imported from assessment/,
lakebridge_assessment/, or autovista/. This package only needs the
Discovery manifest JSON *file* to already exist on disk; it has zero
import-time dependency on the packages that produced it, so it keeps
working even if assessment/ and lakebridge_assessment/ are deleted later.

Usage: `python3 -m llm_assessment.orchestrator` from migration_codes/,
after a sqlglot Discovery run has already populated
LLM_ASSESSMENT_INPUT_MANIFEST. Requires ANTHROPIC_API_KEY -- without it,
every object is left unscored (see complexity_scorer.py's
skipped_no_client count) rather than guessed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from llm_assessment.complexity_scorer import build_object_complexity
from llm_assessment.config import LlmAssessmentConfig, load_config
from llm_assessment.data_readiness import build_data_readiness
from llm_assessment.infra_sizing import build_infra_sizing
from llm_assessment.llm_client import AnthropicLlmClient, LlmClient
from llm_assessment.logging_setup import configure_logging, logger
from llm_assessment.migration_wave_planner import build_migration_waves
from llm_assessment.output_writer import (
    write_csv_rollup,
    write_manifest_json,
    write_markdown_report,
    write_migration_waves_csv,
    write_object_complexity_csv,
    write_risk_register_csv,
)
from llm_assessment.risk_register import build_risk_register
from llm_assessment.schema import AssessmentManifest
from llm_assessment.security_review import build_security_notes
from llm_assessment.summary import build_summary

_DISCLAIMER = (
    "Every complexity tier in this report was assigned by an LLM (see 'model=' in each "
    "object's scoring_reasons) reasoning over Discovery metadata -- NOT measured, NOT "
    "independently verified, and NOT a substitute for a heuristic or vendor-native report. "
    "Treat every tier as a starting point for human review, never as ground truth. "
    "Infra-sizing recommendations are deterministic (not LLM-judged) but are still a "
    "capacity-planning starting point, not a committed spec -- see infra_sizing.py."
)


def _load_discovery_manifest(path: str) -> dict:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Discovery manifest not found at {manifest_path}. Run the Discovery phase "
            f"(python3 -m autovista.orchestrator) first, or set LLM_ASSESSMENT_INPUT_MANIFEST."
        )
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_database_name(discovery_manifest: dict) -> str:
    databases = discovery_manifest.get("databases") or []
    return databases[0].get("name", "unknown") if databases else "unknown"


def build_llm_client(config: LlmAssessmentConfig) -> LlmClient | None:
    if not config.enabled:
        return None
    return AnthropicLlmClient(api_key=config.api_key, model=config.model)


def run_assessment(config: LlmAssessmentConfig | None = None, client: LlmClient | None = None) -> AssessmentManifest:
    config = config or load_config()
    configure_logging(config.output_dir)

    logger.info("=== LLM Assessment run starting: input=%s model=%s ===", config.input_manifest_path, config.model)
    discovery_manifest = _load_discovery_manifest(config.input_manifest_path)
    database = _resolve_database_name(discovery_manifest)

    client = client if client is not None else build_llm_client(config)
    if client is None:
        logger.warning("No ANTHROPIC_API_KEY configured -- every object will be left unscored.")

    object_complexity, llm_stats = build_object_complexity(discovery_manifest, config, client)
    risk_register = build_risk_register(discovery_manifest)
    migration_waves = build_migration_waves(discovery_manifest, object_complexity)
    data_readiness = build_data_readiness(discovery_manifest)
    security_notes = build_security_notes(discovery_manifest)
    infra_sizing = build_infra_sizing(discovery_manifest)
    summary = build_summary(database, object_complexity, risk_register, migration_waves, config)

    warnings = [_DISCLAIMER, f"LLM call stats: {llm_stats}"]
    if llm_stats["skipped_no_client"]:
        warnings.append(
            f"{llm_stats['skipped_no_client']} object(s) were not scored at all: no ANTHROPIC_API_KEY configured."
        )
    if llm_stats["skipped_capped"]:
        warnings.append(
            f"{llm_stats['skipped_capped']} object(s) were not scored: exceeded max_objects_per_run="
            f"{config.max_objects_per_run}. Raise LLM_ASSESSMENT_MAX_OBJECTS_PER_RUN to cover them."
        )
    if llm_stats["failed"]:
        warnings.append(f"{llm_stats['failed']} object(s) failed their LLM call (network/parse error) and were left unscored.")

    manifest = AssessmentManifest(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_manifest_path=config.input_manifest_path,
        database=database,
        object_complexity=object_complexity,
        risk_register=risk_register,
        migration_waves=migration_waves,
        data_readiness=data_readiness,
        security_notes=security_notes,
        infra_sizing=infra_sizing,
        summary=summary,
        warnings=warnings,
    )

    manifest_path = write_manifest_json(manifest, config.output_dir)
    rollup_path = write_csv_rollup(manifest, config.output_dir)
    risk_csv_path = write_risk_register_csv(manifest, config.output_dir)
    complexity_csv_path = write_object_complexity_csv(manifest, config.output_dir)
    waves_csv_path = write_migration_waves_csv(manifest, config.output_dir)
    report_path = write_markdown_report(manifest, config.output_dir)

    logger.info(
        "=== LLM Assessment run finished: %d objects scored (%s), %d risk findings, %d migration waves, %d infra recommendations ===",
        len(object_complexity), llm_stats, len(risk_register), len(migration_waves), len(infra_sizing),
    )
    logger.info(
        "Wrote manifest=%s rollup=%s risk_register=%s object_complexity=%s migration_waves=%s report=%s",
        manifest_path, rollup_path, risk_csv_path, complexity_csv_path, waves_csv_path, report_path,
    )

    return manifest


if __name__ == "__main__":
    run_assessment()
