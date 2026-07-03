"""
Thin subprocess wrapper around the real `databricks labs lakebridge analyze`
CLI -- Lakebridge is a separate Databricks Labs tool (Databricks CLI +
Databricks workspace + Java 21, see README.md "Installing Lakebridge"), not
a Python library this project imports, so shelling out is the integration
surface.

Never invokes any SQL-conversion/transpile Lakebridge subcommand (e.g.
`install-transpile`, `transpile`) -- Discovery only calls `analyze`.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from lakebridge_discovery.config import LakebridgeConfig
from lakebridge_discovery.logging_setup import logger
from lakebridge_discovery.schema import AnalyzeInvocationEntity


def _cli_available(cli_path: str) -> bool:
    return shutil.which(cli_path) is not None


def run_analyze(config: LakebridgeConfig, source_directory: Path, source_tech: str, report_dir: Path) -> AnalyzeInvocationEntity:
    """Runs one `databricks labs lakebridge analyze` invocation for a single
    source-tech (the CLI takes one --source-tech per run, so SQL and SSIS
    exports are analyzed as separate invocations and merged afterwards).
    Never raises -- failures (missing CLI, missing workspace auth, timeout,
    non-zero exit) are captured on the returned entity so one failed
    invocation doesn't abort the whole Lakebridge Discovery run."""

    report_dir.mkdir(parents=True, exist_ok=True)
    # source_tech (e.g. "MS SQL Server") is the exact CLI-required label and
    # may contain spaces -- keep it out of the report filename, since a space
    # there breaks the Analyzer's own internal JSON-companion-file lookup.
    report_slug = source_tech.replace(" ", "_")
    report_path = report_dir / f"lakebridge_report_{report_slug}.xlsx"
    json_path = report_dir / f"lakebridge_report_{report_slug}.json"

    command = [
        config.cli_path, "labs", "lakebridge", "analyze",
        "--source-directory", str(source_directory),
        "--report-file", str(report_path),
        "--source-tech", source_tech,
    ]
    if config.generate_json:
        command += ["--generate-json", "true"]

    entity = AnalyzeInvocationEntity(source_tech=source_tech, command=command, status="failed")

    has_files = source_directory.exists() and any(source_directory.iterdir())
    if not has_files:
        entity.status = "skipped"
        entity.error = f"no exported source files for source-tech={source_tech}, skipping analyze"
        logger.info("SKIP analyze source_tech=%s reason=%s", source_tech, entity.error)
        return entity

    if not _cli_available(config.cli_path):
        entity.status = "failed"
        entity.error = (
            f"'{config.cli_path}' CLI not found on PATH. Install the Databricks CLI and run "
            f"'databricks labs install lakebridge' first -- see README.md 'Installing Lakebridge'."
        )
        logger.error("FAIL analyze source_tech=%s error=%s", source_tech, entity.error)
        return entity

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=config.analyze_timeout_seconds,
            stdin=subprocess.DEVNULL,
        )
        entity.duration_seconds = round(time.perf_counter() - start, 2)
        entity.exit_code = proc.returncode
        entity.stderr_tail = (proc.stderr or "")[-2000:]

        if proc.returncode == 0:
            entity.status = "success"
            if report_path.exists():
                entity.report_excel_path = str(report_path)
            if json_path.exists():
                entity.report_json_path = str(json_path)
            logger.info(
                "OK   analyze source_tech=%-6s exit=0 (%.1fs) report=%s",
                source_tech, entity.duration_seconds, entity.report_excel_path or entity.report_json_path,
            )
        else:
            entity.status = "failed"
            entity.error = f"exit code {proc.returncode}: {entity.stderr_tail}"
            logger.error("FAIL analyze source_tech=%s exit=%s error=%s", source_tech, proc.returncode, entity.error)
    except subprocess.TimeoutExpired:
        entity.duration_seconds = round(time.perf_counter() - start, 2)
        entity.status = "failed"
        entity.error = f"analyze timed out after {config.analyze_timeout_seconds}s"
        logger.error("FAIL analyze source_tech=%s error=%s", source_tech, entity.error)
    except Exception as exc:  # noqa: BLE001 - isolate: a broken CLI invocation must not crash the run
        entity.duration_seconds = round(time.perf_counter() - start, 2)
        entity.status = "failed"
        entity.error = f"{type(exc).__name__}: {exc}"
        logger.error("FAIL analyze source_tech=%s error=%s", source_tech, entity.error)

    return entity
