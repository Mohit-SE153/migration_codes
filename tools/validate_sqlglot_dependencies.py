#!/usr/bin/env python
"""
Dependency Coverage Validation Tool.

Compares the SQLGlot Discovery engine's already-written dependency graph
(output/dependencies.json) against SQL Server's own dependency catalog
(sys.sql_expression_dependencies, plus sys.foreign_keys/sys.synonyms/
sys.triggers for the categories that catalog view doesn't track) and
reports coverage, missing dependencies, out-of-scope noise, and formatting-
only differences.

This tool is entirely separate from and read-only against the production
SQLGlot Discovery engine (autovista/), Lakebridge Discovery, and the
Comparison Engine -- it never writes to output/, never modifies any
autovista/ module, and only imports a handful of already-existing,
read-only helpers (config loading, the live connection builder, and two
production SQL query constants reused verbatim for the Foreign Key/
Synonym/Trigger ground truth -- see tools/dependency_validator/
sql_server_catalog.py's module docstring for exactly what's reused vs.
newly written).

Usage:
    python tools/validate_sqlglot_dependencies.py

Requires the same .env (AUTOVISTA_SQL_*) the production pipeline already
uses, and a previously-run `python -m autovista.orchestrator` (live mode)
so output/dependencies.json exists to compare against.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from autovista.config import load_config  # noqa: E402
from tools.dependency_validator import classify, report, sql_server_catalog, sqlglot_reader  # noqa: E402

# Anchored to the repo root (not left as a bare relative string) so reports
# always land in the same place -- D:\autovista\output_validation\ -- no
# matter what directory this script is actually invoked from. A bare
# relative "output_validation" previously meant the reports were created
# relative to the process's current working directory at invocation time,
# which silently put them somewhere other than the repo root whenever the
# command wasn't run from exactly there.
OUTPUT_VALIDATION_DIR = str(REPO_ROOT / "output_validation")


def run() -> dict:
    config = load_config()
    database = config.source.database

    snapshot = sqlglot_reader.load_sqlglot_snapshot(config.output_dir)
    if not snapshot.dependencies:
        raise SystemExit(
            f"No dependencies found in {config.output_dir}/dependencies.json -- "
            "run `python -m autovista.orchestrator` (AUTOVISTA_RUN_MODE=live) first."
        )

    connection = sql_server_catalog.connect(config)

    sqlglot_keys_strict, sqlglot_keys_loose = classify.build_sqlglot_key_sets(snapshot.dependencies, home_database=database)

    classified: list[tuple[classify.ClassifiedDependency, str | None]] = []

    for row in sql_server_catalog.fetch_expression_dependencies(connection, database):
        dep = classify.classify_expression_dependency(
            row, snapshot.constraint_full_id, snapshot.dynamic_sql_objects,
            sqlglot_keys_strict, sqlglot_keys_loose, database,
        )
        classified.append((dep, row.referencing_type))

    for row in sql_server_catalog.fetch_foreign_keys(connection, database):
        dep = classify.classify_foreign_key(row, sqlglot_keys_strict, sqlglot_keys_loose, database)
        classified.append((dep, None))

    for row in sql_server_catalog.fetch_synonyms(connection, database):
        dep = classify.classify_synonym(row, sqlglot_keys_strict, sqlglot_keys_loose, database)
        classified.append((dep, None))

    for row in sql_server_catalog.fetch_trigger_fires_on(connection, database):
        dep = classify.classify_trigger_fires_on(row, sqlglot_keys_strict, sqlglot_keys_loose, database)
        classified.append((dep, None))

    result = report.write_reports(classified, snapshot.dependencies, OUTPUT_VALIDATION_DIR)
    report.print_console_summary(result)
    return result


if __name__ == "__main__":
    run()
