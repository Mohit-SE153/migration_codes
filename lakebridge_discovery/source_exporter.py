"""
Stages the source database's raw definitions into a directory the Lakebridge
Analyzer CLI can scan (`databricks labs lakebridge analyze --source-directory
...`) -- the Analyzer itself has no live-database connection mode, only a
file-directory input, so this is the file-export step live mode needs.

This module is intentionally independent of autovista/sql_metadata_extractor.py
and autovista/ssis_catalog_extractor.py: it opens its own pyodbc connection
and issues its own catalog queries. The *only* thing shared with the SQLGlot
engine is which source database to point at (same AUTOVISTA_SQL_* env vars,
same fixtures/ sample content) -- never any parsed/derived result. This is a
raw text export (verbatim CREATE-equivalent SQL / .dtsx XML bytes), not a
discovery activity, so staging it here does not make Lakebridge depend on
SQLGlot's discovery output.

Table DDL is NOT natively stored as text in SQL Server (unlike views/procs/
functions/triggers, which sys.sql_modules gives verbatim) so it is
reconstructed from INFORMATION_SCHEMA.COLUMNS on a best-effort basis --
good enough for the Analyzer to see column names/types, not guaranteed to
be a byte-perfect CREATE TABLE statement (no defaults/constraints/indexes).
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from lakebridge_discovery.config import LakebridgeConfig
from lakebridge_discovery.logging_setup import logger
from lakebridge_discovery.schema import ExportSummaryEntity, LakebridgeLogEntry

QUERY_TABLE_LIST = """
SELECT s.name AS schema_name, t.name AS table_name
FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
ORDER BY s.name, t.name
"""

QUERY_TABLE_COLUMNS = """
SELECT c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE, c.CHARACTER_MAXIMUM_LENGTH,
       c.NUMERIC_PRECISION, c.NUMERIC_SCALE
FROM INFORMATION_SCHEMA.COLUMNS c
WHERE c.TABLE_SCHEMA = ? AND c.TABLE_NAME = ?
ORDER BY c.ORDINAL_POSITION
"""

# sys.sql_modules.definition holds the verbatim CREATE/ALTER text for
# procedures, views, functions, and trigger bodies -- one query covers all
# four object kinds since they all live in sys.objects + sys.sql_modules.
QUERY_MODULE_DEFINITIONS = """
SELECT s.name AS schema_name, o.name AS object_name, o.type_desc, m.definition
FROM sys.sql_modules m
JOIN sys.objects o ON o.object_id = m.object_id
JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE o.type IN ('P', 'V', 'FN', 'IF', 'TF', 'TR')
"""

QUERY_SSISDB_EXISTS = "SELECT 1 FROM sys.databases WHERE name = 'SSISDB'"

QUERY_SSISDB_PROJECTS = """
SELECT f.name AS folder_name, p.name AS project_name
FROM SSISDB.catalog.projects p
JOIN SSISDB.catalog.folders f ON f.folder_id = p.folder_id
"""

QUERY_SSISDB_PACKAGES = """
SELECT pkg.name AS package_name
FROM SSISDB.catalog.packages pkg
JOIN SSISDB.catalog.projects p ON p.project_id = pkg.project_id
JOIN SSISDB.catalog.folders f ON f.folder_id = p.folder_id
WHERE f.name = ? AND p.name = ?
"""

QUERY_SSISDB_GET_PROJECT_STREAM = """
DECLARE @project_stream VARBINARY(MAX);
EXEC SSISDB.catalog.get_project @folder_name = ?, @project_name = ?, @project_stream = @project_stream OUTPUT;
SELECT @project_stream;
"""


def _connect_live_sql(config: LakebridgeConfig):
    import pyodbc  # optional dependency -- only required for live mode

    src = config.source
    if not src.is_configured:
        raise RuntimeError(
            "Lakebridge Discovery live mode requires AUTOVISTA_SQL_HOST and "
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


def _sql_type_literal(data_type: str, char_len, num_prec, num_scale) -> str:
    if data_type in ("varchar", "nvarchar", "char", "nchar", "varbinary"):
        length = "MAX" if char_len == -1 else str(char_len or 1)
        return f"{data_type}({length})"
    if data_type in ("decimal", "numeric") and num_prec is not None:
        return f"{data_type}({num_prec},{num_scale or 0})"
    return data_type


def _reconstruct_table_ddl(connection, schema_name: str, table_name: str) -> str:
    cur = connection.cursor()
    cur.execute(QUERY_TABLE_COLUMNS, schema_name, table_name)
    lines = []
    for col_name, data_type, is_nullable, char_len, num_prec, num_scale in cur.fetchall():
        type_literal = _sql_type_literal(data_type, char_len, num_prec, num_scale)
        null_clause = "NULL" if is_nullable == "YES" else "NOT NULL"
        lines.append(f"    [{col_name}] {type_literal} {null_clause}")
    columns_sql = ",\n".join(lines) if lines else "    -- (no columns found)"
    return f"CREATE TABLE [{schema_name}].[{table_name}] (\n{columns_sql}\n);\n"


def _export_live(config: LakebridgeConfig, sql_dir: Path, ssis_dir: Path) -> ExportSummaryEntity:
    summary = ExportSummaryEntity(export_dir=str(config.source_export_dir))
    connection = _connect_live_sql(config)

    try:
        cur = connection.cursor()
        cur.execute(QUERY_TABLE_LIST)
        tables = cur.fetchall()
        for schema_name, table_name in tables:
            try:
                ddl = _reconstruct_table_ddl(connection, schema_name, table_name)
                (sql_dir / f"table__{schema_name}.{table_name}.sql").write_text(ddl, encoding="utf-8")
                summary.table_ddl_files += 1
            except Exception as exc:  # noqa: BLE001 - one bad table shouldn't stop the export
                msg = f"table {schema_name}.{table_name}: {type(exc).__name__}: {exc}"
                summary.export_errors.append(msg)
                logger.error("FAIL export table_ddl %s error=%s", f"{schema_name}.{table_name}", msg)
    except Exception as exc:  # noqa: BLE001
        summary.export_errors.append(f"table list query failed: {type(exc).__name__}: {exc}")

    try:
        cur = connection.cursor()
        cur.execute(QUERY_MODULE_DEFINITIONS)
        for schema_name, object_name, type_desc, definition in cur.fetchall():
            try:
                kind = (type_desc or "object").lower().replace(" ", "_")
                (sql_dir / f"{kind}__{schema_name}.{object_name}.sql").write_text(definition or "", encoding="utf-8")
                summary.sql_definition_files += 1
            except Exception as exc:  # noqa: BLE001
                msg = f"module {schema_name}.{object_name}: {type(exc).__name__}: {exc}"
                summary.export_errors.append(msg)
                logger.error("FAIL export sql_module %s error=%s", f"{schema_name}.{object_name}", msg)
    except Exception as exc:  # noqa: BLE001
        summary.export_errors.append(f"module definitions query failed: {type(exc).__name__}: {exc}")

    if config.dtsx_fallback_dir and os.path.isdir(config.dtsx_fallback_dir):
        for fname in sorted(os.listdir(config.dtsx_fallback_dir)):
            if fname.endswith(".dtsx"):
                shutil.copy(os.path.join(config.dtsx_fallback_dir, fname), ssis_dir / fname)
                summary.ssis_package_files += 1
    else:
        try:
            cur = connection.cursor()
            cur.execute(QUERY_SSISDB_EXISTS)
            if cur.fetchone() is not None:
                _export_ssisdb_catalog(connection, ssis_dir, summary)
            else:
                logger.info("SSISDB not installed -- no SSIS packages to export for Lakebridge.")
        except Exception as exc:  # noqa: BLE001
            summary.export_errors.append(f"SSISDB export failed: {type(exc).__name__}: {exc}")

    return summary


def _export_ssisdb_catalog(connection, ssis_dir: Path, summary: ExportSummaryEntity) -> None:
    import io
    import zipfile

    cur = connection.cursor()
    cur.execute(QUERY_SSISDB_PROJECTS)
    projects = cur.fetchall()
    for folder_name, project_name in projects:
        cur = connection.cursor()
        cur.execute(QUERY_SSISDB_PACKAGES, folder_name, project_name)
        package_names = [r[0] for r in cur.fetchall()]

        cur = connection.cursor()
        cur.execute(QUERY_SSISDB_GET_PROJECT_STREAM, folder_name, project_name)
        row = cur.fetchone()
        if row is None or row[0] is None:
            summary.export_errors.append(f"no .ispac stream for {folder_name}/{project_name}")
            continue
        ispac_bytes = bytes(row[0])
        try:
            with zipfile.ZipFile(io.BytesIO(ispac_bytes)) as ispac:
                for pkg_name in package_names:
                    entry_name = f"{pkg_name}.dtsx"
                    if entry_name not in ispac.namelist():
                        summary.export_errors.append(f"{entry_name} missing from {folder_name}/{project_name} .ispac")
                        continue
                    xml_bytes = ispac.read(entry_name)
                    (ssis_dir / f"{project_name}__{pkg_name}.dtsx").write_bytes(xml_bytes)
                    summary.ssis_package_files += 1
        except Exception as exc:  # noqa: BLE001
            summary.export_errors.append(f"{folder_name}/{project_name} .ispac unzip failed: {type(exc).__name__}: {exc}")


def _export_fixture(sql_dir: Path, ssis_dir: Path) -> ExportSummaryEntity:
    base_dir = Path(__file__).resolve().parent.parent
    ddl_path = base_dir / "fixtures" / "sql" / "ddl_sample.sql"
    dtsx_dir = base_dir / "fixtures" / "dtsx"

    summary = ExportSummaryEntity()
    if ddl_path.exists():
        shutil.copy(ddl_path, sql_dir / ddl_path.name)
        summary.sql_definition_files += 1
        summary.table_ddl_files += 1  # ddl_sample.sql itself contains all table/proc/view DDL

    if dtsx_dir.is_dir():
        for fname in sorted(os.listdir(dtsx_dir)):
            if fname.endswith(".dtsx"):
                shutil.copy(dtsx_dir / fname, ssis_dir / fname)
                summary.ssis_package_files += 1

    return summary


def export_source(config: LakebridgeConfig) -> tuple[ExportSummaryEntity, list[LakebridgeLogEntry]]:
    """Stages source text/XML for the Analyzer into
    `<source_export_dir>/sql/` and `<source_export_dir>/ssis/`. Returns an
    ExportSummaryEntity plus per-stage log entries (this export step is not
    itself "discovery", but failures in it are logged the same way so a
    broken export is triageable)."""

    start = time.perf_counter()
    export_dir = Path(config.source_export_dir)
    sql_dir = export_dir / "sql"
    ssis_dir = export_dir / "ssis"
    sql_dir.mkdir(parents=True, exist_ok=True)
    ssis_dir.mkdir(parents=True, exist_ok=True)

    log_entries: list[LakebridgeLogEntry] = []
    try:
        if config.run_mode == "fixture":
            summary = _export_fixture(sql_dir, ssis_dir)
        elif config.run_mode == "live":
            summary = _export_live(config, sql_dir, ssis_dir)
        else:
            raise ValueError(f"Unknown AUTOVISTA_RUN_MODE: {config.run_mode!r} (expected 'fixture' or 'live')")

        summary.export_dir = str(export_dir)
        duration_ms = (time.perf_counter() - start) * 1000
        status = "success" if not summary.export_errors else "failed"
        log_entries.append(LakebridgeLogEntry(
            stage="source_export", object_type="source_export", object_name=str(export_dir),
            status="success" if status == "success" else "failed",
            error="; ".join(summary.export_errors) if summary.export_errors else None,
            duration_ms=round(duration_ms, 2),
        ))
        logger.info(
            "OK   source_export sql_files=%d table_ddl=%d ssis_files=%d errors=%d (%.1fms)",
            summary.sql_definition_files, summary.table_ddl_files, summary.ssis_package_files,
            len(summary.export_errors), duration_ms,
        )
        return summary, log_entries
    except Exception as exc:  # noqa: BLE001 - isolate export failure from the rest of the run
        duration_ms = (time.perf_counter() - start) * 1000
        error = f"{type(exc).__name__}: {exc}"
        logger.error("FAIL source_export %s error=%s (%.1fms)", export_dir, error, duration_ms)
        log_entries.append(LakebridgeLogEntry(
            stage="source_export", object_type="source_export", object_name=str(export_dir),
            status="failed", error=error, duration_ms=round(duration_ms, 2),
        ))
        summary = ExportSummaryEntity(export_dir=str(export_dir), export_errors=[error])
        return summary, log_entries
