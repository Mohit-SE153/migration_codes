"""
Lakebridge Assessment orchestrator: reads the Lakebridge Discovery engine's
JSON manifest (./output_lakebridge/lakebridge_manifest.json by default --
see LAKEBRIDGE_ASSESSMENT_INPUT_MANIFEST), maps its native per-object
complexity ratings + other signals into this package's contract, and
writes the Assessment output contract to ./output_lakebridge_assessment/.

Only the Lakebridge engine's own Discovery output is read here, mirroring
assessment/orchestrator.py's equivalent sqlglot-only rule -- the two
Assessment outputs are meant to be compared side by side (run both, diff
output_assessment/ vs output_lakebridge_assessment/), not merged into one.

Usage: `python3 -m lakebridge_assessment.orchestrator` from
migration_codes/, after a Lakebridge Discovery run has already populated
LAKEBRIDGE_ASSESSMENT_INPUT_MANIFEST.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from lakebridge_assessment.complexity_mapper import build_object_complexity
from lakebridge_assessment.config import AssessmentConfig, load_config
from lakebridge_assessment.data_readiness import build_data_readiness
from lakebridge_assessment.logging_setup import configure_logging, logger
from lakebridge_assessment.migration_wave_planner import build_migration_waves
from lakebridge_assessment.output_writer import (
    write_csv_rollup,
    write_manifest_json,
    write_markdown_report,
    write_migration_waves_csv,
    write_object_complexity_csv,
    write_risk_register_csv,
)
from lakebridge_assessment.risk_register import build_risk_register
from lakebridge_assessment.schema import AssessmentManifest
from lakebridge_assessment.security_review import build_security_notes
from lakebridge_assessment.summary import build_summary


def _load_lakebridge_manifest(path: str) -> dict:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Lakebridge Discovery manifest not found at {manifest_path}. Run the Lakebridge Discovery phase "
            f"(python3 -m lakebridge_discovery.orchestrator) first, or set LAKEBRIDGE_ASSESSMENT_INPUT_MANIFEST."
        )
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_database_name(lakebridge_manifest: dict) -> str:
    databases = lakebridge_manifest.get("databases") or []
    if databases:
        return databases[0].get("name", "unknown")
    return "unknown"


def run_assessment(config: AssessmentConfig | None = None) -> AssessmentManifest:
    config = config or load_config()
    configure_logging(config.output_dir)

    logger.info("=== Lakebridge Assessment run starting: input=%s ===", config.input_manifest_path)
    lakebridge_manifest = _load_lakebridge_manifest(config.input_manifest_path)
    database = _resolve_database_name(lakebridge_manifest)

    warnings: list[str] = []
    if len(lakebridge_manifest.get("databases") or []) > 1:
        warnings.append(
            "Lakebridge manifest contains more than one database -- findings are combined across all of them."
        )

    object_complexity, skipped_count = build_object_complexity(lakebridge_manifest, config)
    risk_register = build_risk_register(lakebridge_manifest)
    migration_waves = build_migration_waves(lakebridge_manifest, object_complexity)
    data_readiness = build_data_readiness(lakebridge_manifest)
    security_notes = build_security_notes(lakebridge_manifest)
    summary = build_summary(database, object_complexity, skipped_count, risk_register, migration_waves, config)

    manifest = AssessmentManifest(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_manifest_path=config.input_manifest_path,
        database=database,
        mapping_verified=lakebridge_manifest.get("mapping_verified", False),
        mapping_notes=lakebridge_manifest.get("mapping_notes", ""),
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
        "=== Lakebridge Assessment run finished: %d objects scored (%d skipped, no native complexity), "
        "%d risk findings, %d migration waves ===",
        len(object_complexity), skipped_count, len(risk_register), len(migration_waves),
    )
    logger.info(
        "Wrote manifest=%s rollup=%s risk_register=%s object_complexity=%s migration_waves=%s report=%s",
        manifest_path, rollup_path, risk_csv_path, complexity_csv_path, waves_csv_path, report_path,
    )

    return manifest


if __name__ == "__main__":
    run_assessment()
