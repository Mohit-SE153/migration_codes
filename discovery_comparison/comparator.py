"""
Reads each Discovery engine's already-written output (never their internal
modules) and builds an independent, best-effort comparison. This is the
only module in the project allowed to read both engines' outputs -- SQLGlot
Discovery and Lakebridge Discovery never read each other's output
themselves, only this separate comparison step does, and only after both
have already finished (or failed) independently.

Name-based matching between the two engines' inventories is best-effort:
SQLGlot's object identity (database.schema.name) is exact, but Lakebridge's
report field names are unverified (see lakebridge_discovery/schema.py), so
`_normalize_name` below is a loose normalizer, not a guaranteed-correct
join key. Counts (the primary comparison signal) do not depend on name
matching and are always reliable regardless.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path

from discovery_comparison.config import ComparisonConfig
from discovery_comparison.logging_setup import logger
from discovery_comparison.schema import CategoryComparison, ComparisonResult, EngineRunSummary

_LOG_LINE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_FINISHED_LINE = re.compile(r"finished: scanned=(\d+) skipped_unchanged=(\d+) failed=(\d+)")

# category -> (sqlglot filename, lakebridge filename, sqlglot name-field(s))
_CATEGORY_SPECS: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("tables", "tables.json", "tables.json", ("schema", "name")),
    ("views", "views.json", "views.json", ("schema", "name")),
    ("stored_procedures", "stored_procedures.json", "stored_procedures.json", ("schema", "name")),
    ("functions", "functions.json", "functions.json", ("schema", "name")),
    ("triggers", "triggers.json", "triggers.json", ("schema", "name")),
    ("synonyms", "synonyms.json", "synonyms.json", ("schema", "name")),
    ("ssis_packages", "packages.json", "packages.json", ("project", "name")),
]

_SAMPLE_CAP = 20


def _load_json(path: Path) -> list | dict | None:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._]", "", name).lower()
    parts = [p for p in cleaned.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else cleaned


def _sqlglot_object_names(rows: list[dict], name_fields: tuple[str, ...]) -> set[str]:
    names = set()
    for row in rows:
        raw = ".".join(str(row.get(f, "")) for f in name_fields if row.get(f))
        if raw:
            names.add(_normalize_name(raw))
    return names


def _lakebridge_object_names(rows: list[dict]) -> set[str]:
    return {_normalize_name(str(row.get("name", ""))) for row in rows if row.get("name")}


def _summarize_sqlglot_run(output_dir: Path) -> EngineRunSummary:
    log_path = output_dir / "discovery_run.log"
    csv_path = output_dir / "discovery_log_summary.csv"

    if not log_path.exists() and not any(output_dir.glob("*.json")):
        return EngineRunSummary(engine="sqlglot", status="not_run", notes=[f"no output found under {output_dir}"])

    summary = EngineRunSummary(engine="sqlglot", status="failed")
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        timestamps = [m.group(1) for line in lines if (m := _LOG_LINE_TS.match(line))]
        if timestamps:
            summary.started_at = timestamps[0]
            summary.finished_at = timestamps[-1]
            try:
                start = datetime.fromisoformat(timestamps[0])
                end = datetime.fromisoformat(timestamps[-1])
                summary.duration_seconds = round((end - start).total_seconds(), 2)
            except ValueError:
                pass

        for line in lines:
            match = _FINISHED_LINE.search(line)
            if match:
                summary.status = "success"
                summary.error_count = int(match.group(3))
        for line in lines:
            if "REVIEW" in line:
                summary.warning_count += 1

    if csv_path.exists():
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            failed = sum(1 for row in reader if row.get("status") == "failed")
        summary.error_count = max(summary.error_count, failed)

    return summary


def _summarize_lakebridge_run(output_dir: Path) -> EngineRunSummary:
    manifest_path = output_dir / "lakebridge_manifest.json"
    manifest = _load_json(manifest_path)
    if manifest is None:
        return EngineRunSummary(engine="lakebridge", status="not_run", notes=[f"no manifest found at {manifest_path}"])

    status = manifest.get("status", "failed")
    return EngineRunSummary(
        engine="lakebridge",
        status=status,
        duration_seconds=manifest.get("duration_seconds"),
        started_at=manifest.get("started_at"),
        finished_at=manifest.get("finished_at"),
        error_count=len(manifest.get("errors", [])),
        warning_count=len(manifest.get("warnings", [])),
        notes=[manifest.get("mapping_notes", "")] if not manifest.get("mapping_verified", False) else [],
    )


def _compare_category(category: str, sqlglot_file: str, lakebridge_file: str, name_fields: tuple[str, ...],
                       sqlglot_dir: Path, lakebridge_dir: Path) -> CategoryComparison:
    sqlglot_rows = _load_json(sqlglot_dir / sqlglot_file) or []
    lakebridge_rows = _load_json(lakebridge_dir / lakebridge_file) or []

    sqlglot_names = _sqlglot_object_names(sqlglot_rows, name_fields)
    lakebridge_names = _lakebridge_object_names(lakebridge_rows)

    matched = sqlglot_names & lakebridge_names
    sqlglot_only = sorted(sqlglot_names - lakebridge_names)
    lakebridge_only = sorted(lakebridge_names - sqlglot_names)

    return CategoryComparison(
        category=category,
        sqlglot_count=len(sqlglot_rows),
        lakebridge_count=len(lakebridge_rows),
        difference=len(sqlglot_rows) - len(lakebridge_rows),
        matched_count=len(matched),
        sqlglot_only_sample=sqlglot_only[:_SAMPLE_CAP],
        lakebridge_only_sample=lakebridge_only[:_SAMPLE_CAP],
    )


def _unsupported_count_sqlglot(sqlglot_dir: Path) -> int:
    procs = _load_json(sqlglot_dir / "stored_procedures.json") or []
    count = sum(1 for p in procs if p.get("parse_status") in ("unresolved", "llm_inferred") or p.get("unresolved_reason"))
    packages = _load_json(sqlglot_dir / "packages.json") or []
    for pkg in packages:
        for embedded in pkg.get("embedded_sql", []):
            if embedded.get("parse_status") in ("unresolved", "llm_inferred") or embedded.get("unresolved_reason"):
                count += 1
    return count


def build_comparison(config: ComparisonConfig) -> ComparisonResult:
    sqlglot_dir = Path(config.sqlglot_output_dir)
    lakebridge_dir = Path(config.lakebridge_output_dir)

    result = ComparisonResult(generated_at=datetime.utcnow().isoformat() + "Z")
    result.sqlglot_run = _summarize_sqlglot_run(sqlglot_dir)
    result.lakebridge_run = _summarize_lakebridge_run(lakebridge_dir)

    for category, sqlglot_file, lakebridge_file, name_fields in _CATEGORY_SPECS:
        result.categories.append(
            _compare_category(category, sqlglot_file, lakebridge_file, name_fields, sqlglot_dir, lakebridge_dir)
        )

    sqlglot_deps = _load_json(sqlglot_dir / "dependencies.json") or []
    lakebridge_deps = _load_json(lakebridge_dir / "dependencies.json") or []
    result.sqlglot_dependency_count = len(sqlglot_deps)
    result.lakebridge_dependency_count = len(lakebridge_deps)
    result.categories.append(CategoryComparison(
        category="dependencies", sqlglot_count=len(sqlglot_deps), lakebridge_count=len(lakebridge_deps),
        difference=len(sqlglot_deps) - len(lakebridge_deps),
        match_basis="count-only -- dependency edges are not name-matched",
    ))

    sqlglot_unsupported = _unsupported_count_sqlglot(sqlglot_dir)
    lakebridge_unsupported = _load_json(lakebridge_dir / "unsupported_objects.json") or []
    result.categories.append(CategoryComparison(
        category="unsupported_objects", sqlglot_count=sqlglot_unsupported, lakebridge_count=len(lakebridge_unsupported),
        difference=sqlglot_unsupported - len(lakebridge_unsupported),
        match_basis="count-only (sqlglot: unresolved/llm_inferred proc+embedded-sql count; lakebridge: Analyzer-flagged unsupported objects)",
    ))

    if result.sqlglot_run.status == "not_run":
        result.notes.append("SQLGlot Discovery output was not found -- run `python -m autovista.orchestrator` first.")
    if result.lakebridge_run.status == "not_run":
        result.notes.append("Lakebridge Discovery output was not found -- run `python -m lakebridge_discovery.orchestrator` first.")
    if result.sqlglot_run.status == "failed":
        result.notes.append("SQLGlot Discovery run appears to have failed/crashed before completion.")
    if result.lakebridge_run.status in ("failed", "partial"):
        result.notes.append(f"Lakebridge Discovery run status is '{result.lakebridge_run.status}' -- see its errors/warnings.")

    logger.info(
        "Built comparison: sqlglot_status=%s lakebridge_status=%s categories=%d",
        result.sqlglot_run.status, result.lakebridge_run.status, len(result.categories),
    )
    return result
