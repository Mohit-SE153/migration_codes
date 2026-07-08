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
#
# This list covers every category where BOTH engines write a JSON file of
# named objects with a compatible shape, so a same-object name match (not
# just a count) is possible. It is deliberately NOT the only source of
# comparison categories -- see build_comparison()'s rollup-CSV pass below,
# which auto-discovers every remaining category (summary objects, security
# counts, diagnostics, ...) from each engine's own rollup CSV so this list
# never has to be manually kept in sync with future discovery additions.
_CATEGORY_SPECS: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("tables", "tables.json", "tables.json", ("schema", "name")),
    ("views", "views.json", "views.json", ("schema", "name")),
    ("stored_procedures", "stored_procedures.json", "stored_procedures.json", ("schema", "name")),
    ("functions", "functions.json", "functions.json", ("schema", "name")),
    ("triggers", "triggers.json", "triggers.json", ("schema", "name")),
    ("synonyms", "synonyms.json", "synonyms.json", ("schema", "name")),
    ("ssis_packages", "packages.json", "packages.json", ("project", "name")),
    ("schemas", "schemas.json", "schemas.json", ("name",)),
    ("sequences", "sequences.json", "sequences.json", ("schema", "name")),
    ("user_defined_types", "user_defined_types.json", "user_defined_types.json", ("schema", "name")),
    ("xml_schema_collections", "xml_schema_collections.json", "xml_schema_collections.json", ("schema", "name")),
    ("agent_jobs", "agent_jobs.json", "agent_jobs.json", ("name",)),
    ("clr_assemblies", "assemblies.json", "assemblies.json", ("schema", "name")),
    ("indexes", "indexes.json", "indexes.json", ("schema", "table", "name")),
    ("constraints", "constraints.json", "constraints.json", ("schema", "table", "name")),
]

# Each engine's rollup CSV uses its own singular object_type strings
# (see autovista/output_writer.py / lakebridge_discovery/output_writer.py).
# This maps those onto the plural category names _CATEGORY_SPECS/the
# dependencies/unsupported_objects special-cases already use, so the
# rollup-driven auto-sync pass below never emits a duplicate row for a
# category that already has richer, name-matched handling.
_ROLLUP_OBJECT_TYPE_TO_CATEGORY: dict[str, str] = {
    "table": "tables", "view": "views", "stored_procedure": "stored_procedures",
    "function": "functions", "trigger": "triggers", "synonym": "synonyms",
    "ssis_package": "ssis_packages", "schema": "schemas", "sequence": "sequences",
    "user_defined_type": "user_defined_types", "xml_schema_collection": "xml_schema_collections",
    "agent_job": "agent_jobs", "clr_assembly": "clr_assemblies", "index": "indexes",
    "constraint": "constraints", "dependency_edge": "dependencies",
    "unsupported_object": "unsupported_objects", "warning": "warnings",
}

# Rollup CSV filenames -- one per engine, each engine's own stable output
# contract (see write_csv_rollup() in each engine's output_writer.py).
_SQLGLOT_ROLLUP_FILENAME = "discovery_rollup.csv"
_LAKEBRIDGE_ROLLUP_FILENAME = "lakebridge_rollup.csv"

# Requirements 4-7: categories that are generated/derived by the discovery
# engine rather than a native SQL Server catalog object -- SSMS has no
# single system view that reproduces them directly. Rendered as their own
# labeled section in the markdown report so a reader doesn't mistake a
# count difference here for the same kind of discrepancy as, say, a table
# count difference (which SSMS *can* verify directly).
GENERATED_ARTIFACT_NOTES: dict[str, str] = {
    "database_summary": (
        "Generated JSON summary object, assembled from several independent SQL Server catalog "
        "views (sys.databases, sys.tables, sys.indexes, sys.foreign_keys, sys.database_principals, "
        "DATABASEPROPERTYEX, ...). It is NOT a native SQL Server catalog object -- there is no single "
        "sys.database_summary view -- so it cannot be reproduced with one query in SSMS, only "
        "re-derived by composing the same set of views the discovery engine already queries."
    ),
    "data_quality_summary": (
        "Generated JSON object, computed entirely by the discovery engine from SQL Server metadata "
        "already collected (sys.tables/sys.columns/sys.indexes/sys.foreign_keys/sys.triggers/...). It "
        "does not exist as a native SQL Server object and cannot be queried directly from SSMS with a "
        "single system view -- only approximately reproduced by re-deriving the same per-table/"
        "per-column heuristics by hand."
    ),
    "unsupported_objects": (
        "Generated by each discovery engine, not SQL Server -- and the two engines report a "
        "genuinely different concept under the same name. SQLGlot's unsupported_objects reflects "
        "parser-level outcomes: an object where its own T-SQL text either failed to parse "
        "(parse_status='unresolved') or only partially parsed (a non-null unresolved_reason). "
        "Lakebridge's reflects the Databricks Analyzer's migration-feasibility assessment (its "
        "report's own 'unsupported' inventory category) -- a judgment about whether the object can be "
        "converted to run on Databricks, independent of whether the source T-SQL parses cleanly. SQL "
        "Server itself has no catalog object representing 'unsupported for migration' in either sense, "
        "so neither count can be verified via an SSMS system-catalog query -- only by re-running the "
        "respective engine's own parser/analyzer."
    ),
    "warnings": (
        "Generated by each discovery engine's own pipeline, not SQL Server metadata. SQLGlot's "
        "warnings are per-object parser/lineage degradation notices (the same condition as its "
        "unsupported_objects, phrased as a message instead of a count). Lakebridge's warnings are "
        "per-pipeline-stage operational notices (a missing export directory, an unreadable file, a "
        "failed catalog probe connection) -- a different granularity entirely. Neither is queryable "
        "from SSMS; both are artifacts of that run's own discovery process."
    ),
}

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


def _read_rollup_counts(path: Path) -> dict[str, int]:
    """object_type -> summed count, from either engine's own rollup CSV.
    Summing (rather than assuming one row per object_type) is what makes
    this correct for object_types that legitimately appear on multiple
    rows -- e.g. "database" (one row per database on SQLGlot's side) or
    "compatibility_flag" (one row per distinct flag name on both sides)."""
    if not path.exists():
        return {}
    totals: dict[str, int] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            object_type = row.get("object_type")
            count_raw = row.get("count")
            if not object_type or count_raw in (None, ""):
                continue
            try:
                count = int(float(count_raw))
            except ValueError:
                continue
            totals[object_type] = totals.get(object_type, 0) + count
    return totals


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

    # dependency_stats.json is now written by both engines (each engine's
    # own recomputation over its own dependencies.json -- see
    # autovista/dependency_stats.py and
    # lakebridge_discovery/catalog_metadata's _compute_stats) -- read
    # verbatim, never recomputed here, so a relationship_type/
    # discovery_method breakdown is visible in the comparison report
    # without this module re-deriving it from either engine's edge list.
    result.sqlglot_dependency_stats = _load_json(sqlglot_dir / "dependency_stats.json") or {}
    result.lakebridge_dependency_stats = _load_json(lakebridge_dir / "dependency_stats.json") or {}

    # unsupported_objects.json now exists on both engines (see
    # autovista/unsupported_objects.py, added for Lakebridge parity) --
    # read each engine's own real list directly rather than this module
    # recomputing a narrower proxy (the previous version of this function
    # only checked stored_procedures.json + packages.json embedded_sql,
    # silently undercounting relative to the real list once views/
    # functions/triggers/constraints started contributing too -- exactly
    # the "comparison report fell behind the discovery engine" drift this
    # rewrite closes).
    sqlglot_unsupported = _load_json(sqlglot_dir / "unsupported_objects.json") or []
    lakebridge_unsupported = _load_json(lakebridge_dir / "unsupported_objects.json") or []
    result.categories.append(CategoryComparison(
        category="unsupported_objects", sqlglot_count=len(sqlglot_unsupported), lakebridge_count=len(lakebridge_unsupported),
        difference=len(sqlglot_unsupported) - len(lakebridge_unsupported),
        match_basis="count-only (sqlglot: parser-unresolved/degraded objects; lakebridge: Analyzer-flagged unsupported objects -- different concepts, see category_notes)",
    ))

    # --- Auto-sync pass: every remaining category either engine's own
    # rollup CSV knows about (summary objects, security counts, agent
    # jobs/CLR assemblies not already name-matched above, diagnostics,
    # compatibility flags/notes, ...) is picked up here automatically, by
    # reading each engine's already-written rollup rather than hardcoding
    # a category list -- this is what keeps this report synchronized with
    # future discovery additions on either side with zero code changes
    # here: a new rollup row on either engine appears in the next
    # comparison run without touching this module.
    known_categories = {c.category for c in result.categories}
    sqlglot_rollup = _read_rollup_counts(sqlglot_dir / _SQLGLOT_ROLLUP_FILENAME)
    lakebridge_rollup = _read_rollup_counts(lakebridge_dir / _LAKEBRIDGE_ROLLUP_FILENAME)
    all_object_types = sorted(set(sqlglot_rollup) | set(lakebridge_rollup))
    for object_type in all_object_types:
        category = _ROLLUP_OBJECT_TYPE_TO_CATEGORY.get(object_type, object_type)
        if category in known_categories:
            continue
        known_categories.add(category)
        sqlglot_count = sqlglot_rollup.get(object_type, 0)
        lakebridge_count = lakebridge_rollup.get(object_type, 0)
        result.categories.append(CategoryComparison(
            category=category, sqlglot_count=sqlglot_count, lakebridge_count=lakebridge_count,
            difference=sqlglot_count - lakebridge_count,
            match_basis="count-only, from each engine's own rollup CSV -- no per-object name list to match against",
        ))

    # Requirements 4-7: attach the generated-artifact explanation for every
    # category this run actually produced, on either engine.
    for category in known_categories:
        if category in GENERATED_ARTIFACT_NOTES:
            result.category_notes[category] = GENERATED_ARTIFACT_NOTES[category]

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
