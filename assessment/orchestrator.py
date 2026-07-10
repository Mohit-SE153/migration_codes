"""
Assessment-phase orchestrator: reads the sqlglot Discovery engine's JSON
manifest (./output/discovery_manifest.json by default -- see
ASSESSMENT_INPUT_MANIFEST), runs every scoring/rollup module, and writes
the Assessment output contract.

Only the sqlglot-engine Discovery output is read here, never Lakebridge's
(./output_lakebridge/) -- the two engines' manifests are structurally
different schemas (see discovery_comparison/) and mixing them into one
Assessment pass is out of scope for this build.

Usage: `python3 -m assessment.orchestrator` from migration_codes/, after a
Discovery run has already populated ASSESSMENT_INPUT_MANIFEST.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from assessment.complexity_scorer import build_object_complexity
from assessment.config import AssessmentConfig, load_config
from assessment.data_readiness import build_data_readiness
from assessment.logging_setup import configure_logging, logger
from assessment.migration_wave_planner import build_migration_waves
from assessment.output_writer import (
    write_csv_rollup,
    write_manifest_json,
    write_markdown_report,
    write_migration_waves_csv,
    write_object_complexity_csv,
    write_risk_register_csv,
)
from assessment.risk_register import build_risk_register
from assessment.schema import AssessmentManifest
from assessment.security_review import build_security_notes
from assessment.summary import build_summary


def _load_discovery_manifest(path: str) -> dict:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Discovery manifest not found at {manifest_path}. Run the Discovery phase "
            f"(python3 -m autovista.orchestrator) first, or set ASSESSMENT_INPUT_MANIFEST."
        )
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_database_name(discovery_manifest: dict) -> str:
    databases = discovery_manifest.get("databases") or []
    if databases:
        return databases[0].get("name", "unknown")
    return "unknown"


def run_assessment(config: AssessmentConfig | None = None) -> AssessmentManifest:
    config = config or load_config()
    configure_logging(config.output_dir)

    logger.info("=== Assessment run starting: input=%s ===", config.input_manifest_path)
    discovery_manifest = _load_discovery_manifest(config.input_manifest_path)
    database = _resolve_database_name(discovery_manifest)

    warnings: list[str] = []
    if len(discovery_manifest.get("databases") or []) > 1:
        warnings.append(
            "Discovery manifest contains more than one database -- object_complexity/risk_register/"
            "migration_waves cover all of them together, but data_readiness/security_notes counts are "
            "summed across databases rather than broken out per-database."
        )

    object_complexity = build_object_complexity(discovery_manifest, config)
    risk_register = build_risk_register(discovery_manifest)
    migration_waves = build_migration_waves(discovery_manifest, object_complexity)
    data_readiness = build_data_readiness(discovery_manifest)
    security_notes = build_security_notes(discovery_manifest)
    summary = build_summary(database, object_complexity, risk_register, migration_waves, config)

    manifest = AssessmentManifest(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_manifest_path=config.input_manifest_path,
        database=database,
        object_complexity=object_complexity,
        risk_register=risk_register,
        migration_waves=migration_waves,
        data_readiness=data_readiness,
        security_notes=security_notes,
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
        "=== Assessment run finished: %d objects scored, %d risk findings, %d migration waves ===",
        len(object_complexity), len(risk_register), len(migration_waves),
    )
    logger.info(
        "Wrote manifest=%s rollup=%s risk_register=%s object_complexity=%s migration_waves=%s report=%s",
        manifest_path, rollup_path, risk_csv_path, complexity_csv_path, waves_csv_path, report_path,
    )

    return manifest


if __name__ == "__main__":
    run_assessment()
