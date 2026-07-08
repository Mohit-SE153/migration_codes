"""
Lakebridge Discovery orchestrator: independently stages the source database
into files, runs the real `databricks labs lakebridge analyze` CLI once per
source-tech, and maps its report into this engine's own output contract.

Discovery only -- never invokes any Lakebridge transpile/convert/reconcile
subcommand, never generates migrated SQL. See README.md "Lakebridge
Discovery" for prerequisites and how this differs from SQLGlot Discovery.

Run: `python -m lakebridge_discovery.orchestrator`
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.compatibility_remediation import apply_compatibility_remediation
from lakebridge_discovery.compatibility_scanner import apply_compatibility_flags
from lakebridge_discovery.config import LakebridgeConfig, load_config
from lakebridge_discovery.dependency_extractor import extract_dependencies
from lakebridge_discovery.lakebridge_runner import run_analyze
from lakebridge_discovery.logging_setup import configure_logging, logger
from lakebridge_discovery.output_writer import (
    write_csv_rollup,
    write_dependency_stats,
    write_entity_outputs,
    write_manifest_json,
    write_run_log_summary,
)
from lakebridge_discovery.report_parser import parse_invocation
from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeLogEntry
from lakebridge_discovery.source_exporter import export_source, export_supplementary_metadata


def run_discovery(config: LakebridgeConfig | None = None) -> LakebridgeDiscoveryResult:
    config = config or load_config()
    configure_logging(config.output_dir)

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    run_start = time.perf_counter()
    result = LakebridgeDiscoveryResult(run_id=run_id, started_at=started_at, run_mode=config.run_mode)
    all_log_entries: list[LakebridgeLogEntry] = []

    logger.info("=== Lakebridge Discovery run %s started (mode=%s) ===", run_id, config.run_mode)

    if not config.enabled:
        result.status = "skipped"
        result.finished_at = datetime.now(timezone.utc).isoformat()
        result.duration_seconds = round(time.perf_counter() - run_start, 2)
        logger.info("SKIP Lakebridge Discovery: LAKEBRIDGE_ENABLED=false")
        _write_outputs(result, all_log_entries, config.output_dir)
        return result

    export_summary, export_log_entries = export_source(config)
    all_log_entries.extend(export_log_entries)
    result.export_summary = export_summary

    # Supplementary catalog facts (server instance, table structural
    # flags, proc/function parameters, server-level security) --
    # independent of the file-staging export above, but gathered over the
    # same source_exporter.py connection/fixture path. Never blocks the
    # rest of the run: failures are recorded as log entries, same as
    # export_source() above.
    all_log_entries.extend(export_supplementary_metadata(config, result))

    export_dir = Path(config.source_export_dir)
    invocations = [
        run_analyze(config, export_dir / "sql", config.source_tech_sql, Path(config.output_dir) / "reports"),
        run_analyze(config, export_dir / "ssis", config.source_tech_etl, Path(config.output_dir) / "reports"),
    ]
    result.analyze_invocations = invocations

    for invocation in invocations:
        all_log_entries.append(LakebridgeLogEntry(
            stage="analyze", object_type="analyze_invocation", object_name=invocation.source_tech,
            status="success" if invocation.status in ("success", "skipped") else "failed",
            error=invocation.error, duration_ms=(invocation.duration_seconds or 0) * 1000,
        ))
        parse_invocation(invocation, result)

    extract_dependencies(result, export_dir)
    catalog_metadata.run(config, result)

    # SQL-Server-feature compatibility scan -- runs after
    # extract_dependencies() so the same export_dir is already resolved,
    # and after report_parser.py has populated the object inventory this
    # scan iterates over (result.tables/views/stored_procedures/functions/
    # triggers).
    apply_compatibility_flags(result, export_dir)

    # LLM-assisted remediation notes (compatibility_remediation.py) -- runs
    # after apply_compatibility_flags() so it only sends objects that
    # already have a flag; independent of SQLGlot Discovery's own
    # equivalent step, own enable flag and object cap (see config.py).
    apply_compatibility_remediation(result, export_dir, config)

    statuses = [i.status for i in invocations]
    if any(s == "success" for s in statuses) and not any(s == "failed" for s in statuses):
        result.status = "success"
    elif any(s == "success" for s in statuses):
        result.status = "partial"
    else:
        result.status = "failed"

    result.finished_at = datetime.now(timezone.utc).isoformat()
    result.duration_seconds = round(time.perf_counter() - run_start, 2)

    logger.info(
        "=== Lakebridge Discovery run %s finished: status=%s duration=%.2fs objects=%d dependencies=%d errors=%d warnings=%d ===",
        run_id, result.status, result.duration_seconds,
        len(result.tables) + len(result.views) + len(result.stored_procedures) + len(result.functions)
        + len(result.triggers) + len(result.synonyms) + len(result.schemas) + len(result.packages),
        len(result.dependencies), len(result.errors), len(result.warnings),
    )

    _write_outputs(result, all_log_entries, config.output_dir)
    return result


def _write_outputs(result: LakebridgeDiscoveryResult, log_entries: list[LakebridgeLogEntry], output_dir: str) -> None:
    write_entity_outputs(result, output_dir)
    manifest_path = write_manifest_json(result, output_dir)
    csv_path = write_csv_rollup(result, output_dir)
    log_csv_path = write_run_log_summary(log_entries, output_dir)
    stats_path = write_dependency_stats(result, output_dir)
    logger.info(
        "Wrote manifest=%s rollup=%s log_summary=%s dependency_stats=%s",
        manifest_path, csv_path, log_csv_path, stats_path,
    )


if __name__ == "__main__":
    run_discovery()
