"""
Discovery-phase orchestrator: wires the extractor modules together,
handles idempotent/resumable state, and writes the final manifest.

Run modes:
  - fixture: runs entirely against fixtures/ (no live SQL Server needed).
    This is what `python -m autovista.orchestrator` uses by default here,
    since no live environment is reachable in this build.
  - live: connects to a real SQL Server + SSISDB via pyodbc using
    config.SqlServerConfig. The pyodbc.connect() wiring below (see
    _connect_live_sql) and LiveSsisCatalogSource.get_package_xml's
    catalog.get_project + .ispac-unzip logic are UNVERIFIED against a
    real instance -- no live SQL Server/SSISDB was reachable while
    writing this (see spike/step0_report.md for what actually was
    validated). Confirm both against a real instance before trusting
    live-mode output. Requires `pip install pyodbc` plus a system ODBC
    driver (not a hard dependency of fixture mode, so it's commented out
    in requirements.txt).
  - Single database per run: config.source.database names one target
    database. Discovering multiple databases in one run means invoking
    run_discovery once per database (or extending this loop) -- not
    implemented here since it wasn't exercised at pilot scale.

Idempotency: each object's content fingerprint (proc/view definition
hash, table modify-relevant state, .dtsx file mtime+hash) is checked
against StateStore before re-parsing; unchanged objects are logged as
skipped_unchanged rather than re-processed. Safe to re-run on a schedule.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from autovista.config import AutovistaConfig, load_config
from autovista.data_quality_analyzer import build_data_quality_summary
from autovista.dependency_graph_builder import build_dependency_graph
from autovista.dtsx_xml_parser import parse_dtsx_file
from autovista.llm_fallback_extractor import build_llm_client, extract_with_llm_fallback
from autovista.logging_setup import configure_logging, logger
from autovista.output_writer import write_csv_rollup, write_manifest_json, write_run_log_summary
from autovista.schema import DiscoveryManifest
from autovista.sql_lineage_parser import (
    build_view_entity,
    enrich_constraint,
    enrich_embedded_sql,
    enrich_function,
    enrich_stored_procedure,
    enrich_trigger,
    parse_lineage,
)
from autovista.sql_metadata_extractor import FixtureMetadataSource, LiveSqlServerSource, extract_database_metadata
from autovista.ssis_catalog_extractor import FileSystemDtsxSource, LiveSsisCatalogSource, extract_ssis_packages
from autovista.state_store import StateStore


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _connect_live_sql(config: AutovistaConfig):
    """
    Builds a pyodbc connection from config.source. UNVERIFIED against a
    real instance -- no live SQL Server was reachable while writing this.
    The connection-string shape follows the standard ODBC Driver 18
    format; confirm the Encrypt/TrustServerCertificate combination your
    network requires on first real run (a self-signed or internal CA cert
    commonly needs `TrustServerCertificate=yes` added here).
    """
    import pyodbc  # optional dependency -- only required for live mode

    src = config.source
    if not src.is_configured:
        raise RuntimeError(
            "AUTOVISTA_RUN_MODE=live requires AUTOVISTA_SQL_HOST and "
            "AUTOVISTA_SQL_DATABASE to be set -- see .env.example."
        )

    parts = [
        f"DRIVER={{{src.driver}}}",
        f"SERVER={src.host}",
        f"DATABASE={src.database}",
        f"Encrypt={'yes' if src.encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if src.trust_server_certificate else 'no'}",
    ]
    if src.use_integrated_auth:
        parts.append("Trusted_Connection=yes")
    else:
        if not src.username or not src.password:
            raise RuntimeError(
                "AUTOVISTA_SQL_USERNAME and AUTOVISTA_SQL_PASSWORD must be set "
                "unless AUTOVISTA_SQL_INTEGRATED_AUTH=true."
            )
        parts.append(f"UID={src.username}")
        parts.append(f"PWD={src.password}")

    return pyodbc.connect(";".join(parts) + ";")


def run_discovery(config: AutovistaConfig | None = None) -> DiscoveryManifest:
    config = config or load_config()
    configure_logging(config.output_dir)

    run_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    state = StateStore(config.state_db_path)

    logger.info("=== Autovista Discovery run %s started (mode=%s) ===", run_id, config.run_mode)

    all_log_entries = []
    manifest = DiscoveryManifest()

    if config.run_mode == "fixture":
        from fixtures.mock_catalog import MockCatalog  # fixture-mode only import

        metadata_source = FixtureMetadataSource(catalog=MockCatalog())
        ssis_source = FileSystemDtsxSource(directory=config.dtsx_fallback_dir or "fixtures/dtsx")
        database_name = "SalesDW"
        # Fixture data represents SSISDB-deployed packages by design (see README) even
        # though it's read from disk here, since no live SSISDB was reachable to spike against.
        ssis_deployment_model = "ssisdb"
    elif config.run_mode == "live":
        connection = _connect_live_sql(config)
        metadata_source = LiveSqlServerSource(connection=connection)
        database_name = config.source.database
        if config.dtsx_fallback_dir:
            ssis_source = FileSystemDtsxSource(directory=config.dtsx_fallback_dir, project_name=database_name)
            ssis_deployment_model = "file_system"
        else:
            ssis_source = LiveSsisCatalogSource(connection=connection)
            ssis_deployment_model = "ssisdb"
    else:
        raise ValueError(f"Unknown AUTOVISTA_RUN_MODE: {config.run_mode!r} (expected 'fixture' or 'live')")

    with state.run(run_id, now_iso) as counters:
        db_result, log_entries = extract_database_metadata(metadata_source, database=database_name)
        all_log_entries.extend(log_entries)

        manifest.databases = db_result["databases"]
        manifest.agent_jobs = db_result["agent_jobs"]
        manifest.database_files = db_result.get("database_files", [])
        manifest.indexes = db_result.get("indexes", [])
        manifest.synonyms = db_result.get("synonyms", [])
        manifest.sequences = db_result.get("sequences", [])
        manifest.user_defined_types = db_result.get("user_defined_types", [])
        manifest.xml_schema_collections = db_result.get("xml_schema_collections", [])
        manifest.assemblies = db_result.get("assemblies", [])
        manifest.security_principals = db_result.get("security_principals", [])
        manifest.permissions = db_result.get("permissions", [])
        manifest.database_summary = db_result.get("database_summary", [])
        manifest.constraints = db_result.get("constraints", [])

        # Computed first (before tables below need it for computed-column
        # function detection, and before procs/views/triggers/constraints
        # need it for their own inline function-call detection) -- a call
        # site only ever carries the bare function name, never its schema
        # (see sql_lineage_parser.py).
        known_function_names = frozenset(
            f"{func_entity.schema}.{func_entity.name}" for func_entity, _ in db_result.get("functions", [])
        )

        # --- Tables: direct_metadata, resumable via row/size fingerprint ---
        for table in db_result["tables"]:
            object_id = f"{table.database}.{table.schema}.{table.name}"
            fingerprint = _fingerprint(f"{table.row_count}:{table.size_mb}:{table.column_count}")
            if state.has_changed(object_id, fingerprint):
                manifest.tables.append(table)
                state.record_fingerprint(object_id, "table", fingerprint, run_id, now_iso)
                counters["scanned"] += 1
            else:
                logger.info("SKIP table        %-45s unchanged since last run", object_id)
                counters["skipped_unchanged"] += 1
                manifest.tables.append(table)  # still included in output; just not re-logged as newly scanned

        # --- Computed columns: detect scalar UDF calls inside the computed
        # expression (e.g. `AS (dbo.ufnCalc(Price, Qty))`) -- a computed
        # column can only reference other columns in the same row, never
        # another table, so table-lineage parsing doesn't apply here. ---
        for table in manifest.tables:
            for column in table.columns:
                if column.computed_expression:
                    result = parse_lineage(column.computed_expression, known_function_names=known_function_names)
                    column.referenced_functions = result.referenced_functions

        # --- Data Quality Summary: metadata-only, computed from tables/indexes/constraints above ---
        manifest.data_quality_summary = [
            build_data_quality_summary(database_name, manifest.tables, manifest.indexes, manifest.constraints)
        ]

        # --- Functions: enrich with sqlglot lineage ---
        for func_entity, definition in db_result.get("functions", []):
            enrich_function(func_entity, definition, known_function_names=known_function_names)
            manifest.functions.append(func_entity)
            counters["scanned"] += 1

        # --- Triggers: enrich with sqlglot lineage ---
        for trigger_entity, definition in db_result.get("triggers", []):
            enrich_trigger(trigger_entity, definition, known_function_names=known_function_names)
            manifest.triggers.append(trigger_entity)
            counters["scanned"] += 1

        # --- Constraints: enrich CHECK/DEFAULT definitions with sqlglot
        # lineage (enrich_constraint no-ops for PRIMARY_KEY/UNIQUE/FOREIGN_KEY,
        # which have no expression text) ---
        for constraint_entity in manifest.constraints:
            enrich_constraint(constraint_entity, known_function_names=known_function_names)

        # --- Stored procedures: enrich with sqlglot lineage, resumable via definition hash ---
        llm_client = build_llm_client(config.llm)
        llm_objects_attempted = 0

        for proc_entity, definition in db_result["stored_procedures"]:
            object_id = f"{proc_entity.database}.{proc_entity.schema}.{proc_entity.name}"
            fingerprint = _fingerprint(definition)
            if not state.has_changed(object_id, fingerprint):
                logger.info("SKIP proc         %-45s unchanged since last run", object_id)
                counters["skipped_unchanged"] += 1
                manifest.stored_procedures.append(proc_entity)
                continue

            enrich_stored_procedure(proc_entity, definition, known_function_names=known_function_names)
            if proc_entity.parse_status == "unresolved":
                llm_result = extract_with_llm_fallback(
                    llm_client, object_id, definition, llm_objects_attempted, config.llm
                )
                llm_objects_attempted += 1
                proc_entity.referenced_tables = llm_result.referenced_tables
                proc_entity.referenced_procs = llm_result.referenced_procs
                proc_entity.parse_status = llm_result.parse_status
                proc_entity.unresolved_reason = proc_entity.unresolved_reason or llm_result.notes
                logger.warning(
                    "REVIEW proc       %-45s needs_human_review=%s (%s)",
                    object_id, llm_result.needs_human_review, llm_result.notes,
                )

            manifest.stored_procedures.append(proc_entity)
            state.record_fingerprint(object_id, "stored_procedure", fingerprint, run_id, now_iso)
            counters["scanned"] += 1

        # --- Views: enrich with sqlglot lineage ---
        for view_entry in db_result["views"]:
            if isinstance(view_entry, tuple) and len(view_entry) == 3:
                schema_name, view_name, definition = view_entry
                view_entity = build_view_entity(
                    database=database_name, schema=schema_name, name=view_name, definition=definition,
                    known_function_names=known_function_names,
                )
            else:
                view_entity = view_entry
            manifest.views.append(view_entity)
            counters["scanned"] += 1

        # --- SSIS packages: XML parse + lineage-enrich embedded SQL ---
        packages, ssis_log_entries = extract_ssis_packages(ssis_source, deployment_model=ssis_deployment_model)
        all_log_entries.extend(ssis_log_entries)

        for package in packages:
            object_id = f"{package.project}.{package.name}"

            fingerprint = _fingerprint(ssis_source.get_package_xml(package.folder, package.project, package.name))
            if not state.has_changed(object_id, fingerprint):
                logger.info("SKIP package      %-45s unchanged since last run", object_id)
                counters["skipped_unchanged"] += 1
                manifest.packages.append(package)
                continue
            state.record_fingerprint(object_id, "ssis_package", fingerprint, run_id, now_iso)

            for embedded in package.embedded_sql:
                enrich_embedded_sql(embedded)
                if embedded.parse_status == "unresolved":
                    llm_result = extract_with_llm_fallback(
                        llm_client, f"{object_id}::{embedded.task_name}", embedded.sql_text,
                        llm_objects_attempted, config.llm,
                    )
                    llm_objects_attempted += 1
                    embedded.referenced_tables = llm_result.referenced_tables
                    embedded.referenced_procs = llm_result.referenced_procs
                    embedded.parse_status = llm_result.parse_status
                    embedded.unresolved_reason = embedded.unresolved_reason or llm_result.notes

            # Script Task bodies: no SQL text to lineage-parse, always LLM/unresolved.
            for task in package.tasks:
                if task.unparseable_body:
                    llm_result = extract_with_llm_fallback(
                        llm_client, f"{object_id}::{task.name}", "(script task source withheld from this summary)",
                        llm_objects_attempted, config.llm,
                    )
                    llm_objects_attempted += 1
                    logger.warning(
                        "REVIEW script_task %-44s needs_human_review=True (%s)",
                        f"{object_id}::{task.name}", llm_result.notes,
                    )

            manifest.packages.append(package)
            counters["scanned"] += len(package.tasks) + 1

        # --- Dependency graph: required output, built from everything above ---
        # expression_dependencies (sys.sql_expression_dependencies) is used
        # only to backfill objects whose own sqlglot parse degraded/failed --
        # see dependency_graph_builder.py's module docstring.
        expression_dependencies = metadata_source.list_expression_dependencies(database_name)
        manifest.dependencies = build_dependency_graph(
            stored_procedures=manifest.stored_procedures,
            views=manifest.views,
            packages=manifest.packages,
            foreign_keys=db_result["foreign_keys"],
            functions=manifest.functions,
            triggers=manifest.triggers,
            constraints=manifest.constraints,
            synonyms=manifest.synonyms,
            tables=manifest.tables,
            user_defined_types=manifest.user_defined_types,
            expression_dependencies=expression_dependencies,
        )

        counters["failed"] = sum(1 for e in all_log_entries if e.status == "failed")

    logger.info(
        "=== Discovery run %s finished: scanned=%d skipped_unchanged=%d failed=%d ===",
        run_id, counters["scanned"], counters["skipped_unchanged"], counters["failed"],
    )

    manifest_path = write_manifest_json(manifest, config.output_dir)
    csv_path = write_csv_rollup(manifest, config.output_dir)
    log_csv_path = write_run_log_summary(all_log_entries, config.output_dir)
    logger.info("Wrote manifest=%s rollup=%s log_summary=%s", manifest_path, csv_path, log_csv_path)

    return manifest


if __name__ == "__main__":
    run_discovery()
