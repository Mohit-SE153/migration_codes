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
import re
import shutil
import time
from pathlib import Path

from lakebridge_discovery.config import LakebridgeConfig
from lakebridge_discovery.logging_setup import logger
from lakebridge_discovery.schema import (
    ExportSummaryEntity,
    LakebridgeDiscoveryResult,
    LakebridgeLogEntry,
    LinkedServerEntity,
    ProcedureParameterEntity,
    ServerInstanceEntity,
    ServerPermissionEntity,
    ServerPrincipalEntity,
    TableFeatureEntity,
)

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


# --- Supplementary catalog metadata (server instance / table structural
# flags / proc & function parameters / server-level security) -- retyped
# independently of autovista/sql_metadata_extractor.py's equivalent
# queries, per this codebase's no-shared-parsing/query-logic rule between
# the two Discovery engines (see module docstring and README.md). Fetched
# by export_supplementary_metadata() below over the SAME connection
# _export_live() already opens -- this module is the only place in
# lakebridge_discovery/ that talks to a live SQL Server, so it's the
# natural home for these even though they're not files staged for the
# Analyzer CLI to scan. ---

QUERY_SERVER_PROPERTIES = """
SELECT
    CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128)) AS product_version,
    CAST(SERVERPROPERTY('ProductLevel') AS NVARCHAR(128)) AS product_level,
    CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128)) AS edition,
    CAST(SERVERPROPERTY('EngineEdition') AS INT) AS engine_edition,
    CAST(SERVERPROPERTY('MachineName') AS NVARCHAR(128)) AS machine_name,
    CAST(SERVERPROPERTY('InstanceName') AS NVARCHAR(128)) AS instance_name
"""

# sys.dm_os_sys_info is a sys.dm_* DMV -- requires VIEW SERVER STATE, not
# guaranteed for a read-only discovery account, so callers wrap this in
# try/except.
QUERY_SERVER_SYS_INFO = """
SELECT cpu_count, physical_memory_kb
FROM sys.dm_os_sys_info
"""

QUERY_SERVER_MAX_MEMORY = """
SELECT CAST(value_in_use AS INT) AS max_server_memory_mb
FROM sys.configurations
WHERE name = 'max server memory (MB)'
"""

# sys.tables.temporal_type: 0 = none, 1 = history table, 2 = the
# system-versioned table itself. sys.change_tracking_tables has one row
# per table with change tracking enabled -- existence, not a flag column.
QUERY_TABLE_FEATURES = """
SELECT s.name AS schema_name, t.name AS table_name, t.temporal_type, t.is_memory_optimized, t.is_tracked_by_cdc,
       CASE WHEN ctt.object_id IS NOT NULL THEN 1 ELSE 0 END AS is_change_tracking_enabled
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
LEFT JOIN sys.change_tracking_tables ctt ON ctt.object_id = t.object_id
"""

# Scoped to index_id IN (0, 1) (heap or clustered index -- the table's own
# row storage), same reasoning as autovista's equivalent query: secondary
# nonclustered indexes can carry independent partition/compression
# settings that would misrepresent "how many partitions does this table
# have" if included here.
QUERY_TABLE_PARTITION_COMPRESSION = """
SELECT s.name AS schema_name, t.name AS table_name, p.partition_number, p.data_compression_desc
FROM sys.partitions p
JOIN sys.tables t ON t.object_id = p.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE p.index_id IN (0, 1)
ORDER BY s.name, t.name, p.partition_number
"""

# One whole-database query covering both procedures and functions (P/FN/
# IF/TF all live in sys.objects + sys.parameters) -- parameter_id > 0
# excludes a function's own return-value row.
QUERY_PROCEDURE_FUNCTION_PARAMETERS = """
SELECT s.name AS schema_name, o.name AS object_name, p.name AS parameter_name, ty.name AS data_type, p.is_output
FROM sys.parameters p
JOIN sys.objects o ON o.object_id = p.object_id
JOIN sys.schemas s ON s.schema_id = o.schema_id
JOIN sys.types ty ON ty.user_type_id = p.user_type_id
WHERE p.parameter_id > 0 AND o.type IN ('P', 'FN', 'IF', 'TF')
ORDER BY s.name, o.name, p.parameter_id
"""

# type: 'S' = SQL login, 'U' = Windows login, 'G' = Windows group, 'R' = server role.
QUERY_SERVER_PRINCIPALS = """
SELECT sp.name, sp.type, sp.is_disabled, sp.is_fixed_role
FROM sys.server_principals sp
WHERE sp.type IN ('S', 'U', 'G', 'R')
"""

QUERY_SERVER_ROLE_MEMBERS = """
SELECT role.name AS role_name, member.name AS member_name
FROM sys.server_role_members rm
JOIN sys.server_principals role ON role.principal_id = rm.role_principal_id
JOIN sys.server_principals member ON member.principal_id = rm.member_principal_id
"""

QUERY_SERVER_PERMISSIONS = """
SELECT sp.name AS grantee_name, sp.type AS principal_type, perm.class_desc,
       tgt.name AS object_name, perm.permission_name, perm.state_desc
FROM sys.server_permissions perm
JOIN sys.server_principals sp ON sp.principal_id = perm.grantee_principal_id
LEFT JOIN sys.server_principals tgt ON tgt.principal_id = perm.major_id AND perm.class_desc = 'SERVER_PRINCIPAL'
"""

# is_linked = 1 excludes the row sys.servers always carries for the local
# server itself.
QUERY_LINKED_SERVERS = """
SELECT name, product, provider, data_source, provider_string
FROM sys.servers
WHERE is_linked = 1
"""

# Defensive redaction for sys.servers.provider_string, which can embed a
# password in some OLE DB/ODBC provider connection strings -- retyped
# independently of autovista/sql_metadata_extractor.py's
# _redact_connection_string (and dtsx_xml_parser.py's, which it itself
# mirrors), per this module's no-shared-code rule.
_CONN_STRING_SECRET_PATTERN = re.compile(r"(?i)(password|pwd)\s*=\s*[^;]*")


def _redact_connection_string(conn_str: str) -> str:
    return _CONN_STRING_SECRET_PATTERN.sub(r"\1=***REDACTED***", conn_str)


def _fetch_server_instance(connection) -> ServerInstanceEntity | None:
    cur = connection.cursor()
    cur.execute(QUERY_SERVER_PROPERTIES)
    row = cur.fetchone()
    if row is None:
        return None
    product_version, product_level, edition, engine_edition, machine_name, instance_name = row
    entity = ServerInstanceEntity(
        product_version=product_version,
        product_level=product_level,
        edition=edition,
        engine_edition=int(engine_edition) if engine_edition is not None else None,
        machine_name=machine_name,
        instance_name=instance_name,
    )

    try:
        cur = connection.cursor()
        cur.execute(QUERY_SERVER_SYS_INFO)
        sys_row = cur.fetchone()
        if sys_row:
            cpu_count, physical_memory_kb = sys_row
            entity.cpu_count = int(cpu_count) if cpu_count is not None else None
            entity.physical_memory_mb = (
                round(float(physical_memory_kb) / 1024.0, 2) if physical_memory_kb is not None else None
            )
    except Exception:
        pass  # sys.dm_os_sys_info requires VIEW SERVER STATE -- leave cpu_count/physical_memory_mb at defaults

    try:
        cur = connection.cursor()
        cur.execute(QUERY_SERVER_MAX_MEMORY)
        mem_row = cur.fetchone()
        if mem_row and mem_row[0] is not None:
            entity.max_server_memory_mb = int(mem_row[0])
    except Exception:
        pass  # sys.configurations restricted -- leave max_server_memory_mb at default

    return entity


def _fetch_table_features(connection) -> list[TableFeatureEntity]:
    partitions_by_table: dict[tuple[str, str], list[tuple[int, str]]] = {}
    cur = connection.cursor()
    cur.execute(QUERY_TABLE_PARTITION_COMPRESSION)
    for schema_name, table_name, partition_number, data_compression_desc in cur.fetchall():
        partitions_by_table.setdefault((schema_name, table_name), []).append(
            (partition_number, data_compression_desc)
        )

    cur = connection.cursor()
    cur.execute(QUERY_TABLE_FEATURES)
    out = []
    for schema_name, table_name, temporal_type, is_memory_optimized, is_tracked_by_cdc, is_change_tracking_enabled in cur.fetchall():
        partitions = partitions_by_table.get((schema_name, table_name), [])
        partition_count = len(partitions)
        compression_descs = sorted({desc for _, desc in partitions if desc})
        if len(compression_descs) > 1:
            compression = f"MIXED ({', '.join(compression_descs)})"
        else:
            compression = compression_descs[0] if compression_descs else None
        out.append(
            TableFeatureEntity(
                schema=schema_name,
                name=table_name,
                is_temporal_table=temporal_type in (1, 2),
                is_memory_optimized=bool(is_memory_optimized),
                is_cdc_enabled=bool(is_tracked_by_cdc),
                is_change_tracking_enabled=bool(is_change_tracking_enabled),
                is_partitioned=partition_count > 1,
                partition_count=partition_count,
                compression=compression,
            )
        )
    return out


def _fetch_procedure_parameters(connection) -> list[ProcedureParameterEntity]:
    cur = connection.cursor()
    cur.execute(QUERY_PROCEDURE_FUNCTION_PARAMETERS)
    return [
        ProcedureParameterEntity(
            schema=schema_name, name=object_name, parameter_name=parameter_name,
            data_type=data_type, mode="OUT" if is_output else "IN",
        )
        for schema_name, object_name, parameter_name, data_type, is_output in cur.fetchall()
    ]


def _fetch_server_principals(connection) -> list[ServerPrincipalEntity]:
    role_members: dict[str, list[str]] = {}
    cur = connection.cursor()
    cur.execute(QUERY_SERVER_ROLE_MEMBERS)
    for role_name, member_name in cur.fetchall():
        role_members.setdefault(member_name, []).append(role_name)

    cur = connection.cursor()
    cur.execute(QUERY_SERVER_PRINCIPALS)
    out = []
    for name, type_code, is_disabled, is_fixed_role in cur.fetchall():
        out.append(
            ServerPrincipalEntity(
                name=name,
                principal_type="SERVER_ROLE" if type_code == "R" else "LOGIN",
                is_disabled=bool(is_disabled) if is_disabled is not None else None,
                is_fixed_role=bool(is_fixed_role) if is_fixed_role is not None else None,
                member_of_roles=role_members.get(name, []),
            )
        )
    return out


def _fetch_server_permissions(connection) -> list[ServerPermissionEntity]:
    cur = connection.cursor()
    cur.execute(QUERY_SERVER_PERMISSIONS)
    return [
        ServerPermissionEntity(
            grantee=grantee_name, principal_type=principal_type, class_desc=class_desc,
            object_name=object_name, permission_name=permission_name, state_desc=state_desc,
        )
        for grantee_name, principal_type, class_desc, object_name, permission_name, state_desc in cur.fetchall()
    ]


def _fetch_linked_servers(connection) -> list[LinkedServerEntity]:
    cur = connection.cursor()
    cur.execute(QUERY_LINKED_SERVERS)
    return [
        LinkedServerEntity(
            name=name, product=product, provider=provider, data_source=data_source,
            provider_string_redacted=_redact_connection_string(provider_string) if provider_string else None,
        )
        for name, product, provider, data_source, provider_string in cur.fetchall()
    ]


# --- Fixture-mode supplementary metadata: no live SQL Server to query, so
# these are parsed via plain regex from the same fixtures/sql/ddl_sample.sql
# this engine already stages for the Analyzer (see _export_fixture) --
# same "plain-regex scan, not a SQL parser" convention as
# dependency_extractor.py. Deliberately does NOT import
# fixtures/mock_catalog.py -- that module is SQLGlot Discovery's own
# sqlglot-AST-based fixture parser; reusing it here would blur the
# two-engines boundary for no real benefit, since only table/parameter
# *names* are needed, not a full AST. ---

_GO_SPLIT = re.compile(r"^\s*GO\s*$", re.MULTILINE)
# Unlike CREATE PROCEDURE/TRIGGER (one per GO-separated batch in
# ddl_sample.sql), several CREATE TABLE statements can share one GO batch
# (see the "dbo schema: 15 core tables" block) -- scanned directly against
# the whole file text via finditer, each table's own body captured up to
# its closing ");" rather than assuming one CREATE TABLE per batch.
_CREATE_TABLE_BLOCK = re.compile(
    r"CREATE\s+TABLE\s+(?:\[?(\w+)\]?\.)?\[?(\w+)\]?\s*\((.*?)\)\s*;", re.IGNORECASE | re.DOTALL,
)
_TEMPORAL_MARKER = re.compile(r"PERIOD\s+FOR\s+SYSTEM_TIME|SYSTEM_VERSIONING\s*=\s*ON", re.IGNORECASE)
_MEMORY_OPTIMIZED_MARKER = re.compile(r"MEMORY_OPTIMIZED\s*=\s*ON", re.IGNORECASE)
_DATA_COMPRESSION_MARKER = re.compile(r"DATA_COMPRESSION\s*=\s*(\w+)", re.IGNORECASE)
_CREATE_PROC_HEADER = re.compile(
    r"CREATE\s+PROCEDURE\s+(?:\[?(\w+)\]?\.)?\[?(\w+)\]?\s*(.*?)\bAS\b", re.IGNORECASE | re.DOTALL,
)
_PARAM_TOKEN = re.compile(
    r"@(\w+)\s+([A-Za-z][\w]*(?:\(\s*(?:MAX|\d+)\s*(?:,\s*\d+\s*)?\))?)\s*(OUTPUT|OUT)?", re.IGNORECASE,
)


def _split_ddl_batches(raw_sql: str) -> list[str]:
    return [b.strip() for b in _GO_SPLIT.split(raw_sql) if b.strip()]


def _populate_fixture_supplementary_metadata(result: LakebridgeDiscoveryResult) -> None:
    result.server_instance = ServerInstanceEntity(
        product_version="15.0.4153.1 [FIXTURE DATA]",
        product_level="RTM",
        edition="Developer Edition (64-bit) [FIXTURE DATA]",
        engine_edition=3,
        machine_name="LAKEBRIDGE-FIXTURE-HOST",
        instance_name=None,
        cpu_count=4,
        physical_memory_mb=16384.0,
        max_server_memory_mb=12288,
    )

    base_dir = Path(__file__).resolve().parent.parent
    ddl_path = base_dir / "fixtures" / "sql" / "ddl_sample.sql"
    table_features: list[TableFeatureEntity] = []
    procedure_parameters: list[ProcedureParameterEntity] = []
    if ddl_path.exists():
        raw_sql = ddl_path.read_text(encoding="utf-8")

        for table_match in _CREATE_TABLE_BLOCK.finditer(raw_sql):
            schema_name, table_name, body = table_match.group(1), table_match.group(2), table_match.group(3)
            compression_match = _DATA_COMPRESSION_MARKER.search(body)
            table_features.append(
                TableFeatureEntity(
                    schema=schema_name or "dbo",
                    name=table_name,
                    is_temporal_table=bool(_TEMPORAL_MARKER.search(body)),
                    is_memory_optimized=bool(_MEMORY_OPTIMIZED_MARKER.search(body)),
                    # CDC / change tracking are enabled via
                    # sp_cdc_enable_table / ALTER TABLE ... ENABLE
                    # CHANGE_TRACKING, never part of CREATE TABLE text
                    # itself -- always False from a static DDL scan.
                    is_cdc_enabled=False,
                    is_change_tracking_enabled=False,
                    is_partitioned=False,
                    partition_count=0,
                    compression=compression_match.group(1).upper() if compression_match else None,
                )
            )

        for batch in _split_ddl_batches(raw_sql):
            proc_match = _CREATE_PROC_HEADER.search(batch)
            if proc_match:
                schema_name = proc_match.group(1) or "dbo"
                proc_name = proc_match.group(2)
                for token_match in _PARAM_TOKEN.finditer(proc_match.group(3)):
                    param_name, data_type, output_kw = token_match.groups()
                    procedure_parameters.append(
                        ProcedureParameterEntity(
                            schema=schema_name, name=proc_name, parameter_name=param_name,
                            data_type=data_type, mode="OUT" if output_kw else "IN",
                        )
                    )

    result.table_features = table_features
    result.procedure_parameters = procedure_parameters

    # Server-level security/linked-server fixtures: plausible, clearly
    # synthetic values (no live sys.server_principals/sys.servers to query
    # in fixture mode).
    result.server_principals = [
        ServerPrincipalEntity(
            name="sa", principal_type="LOGIN", is_disabled=False, is_fixed_role=False,
            member_of_roles=["sysadmin"],
        ),
        ServerPrincipalEntity(
            name="sysadmin", principal_type="SERVER_ROLE", is_disabled=None, is_fixed_role=True,
        ),
        ServerPrincipalEntity(
            name="lakebridge_fixture_svc", principal_type="LOGIN", is_disabled=False, is_fixed_role=False,
            member_of_roles=["db_datareader"],
        ),
    ]
    result.server_permissions = [
        ServerPermissionEntity(
            grantee="lakebridge_fixture_svc", principal_type="S", class_desc="SERVER",
            object_name=None, permission_name="VIEW ANY DEFINITION", state_desc="GRANT",
        ),
    ]
    result.linked_servers = []  # no linked servers in the fixture sample environment


def export_supplementary_metadata(config: LakebridgeConfig, result: LakebridgeDiscoveryResult) -> list[LakebridgeLogEntry]:
    """Populates server_instance/table_features/procedure_parameters/
    server_principals/server_permissions/linked_servers on `result`, in
    place -- same "mutate result, return per-stage log entries" shape as
    dependency_extractor.extract_dependencies()/export_source() use.
    These are supplementary catalog facts the Analyzer CLI itself never
    reports (it only ever sees the raw SQL text export_source() stages for
    it) -- gathered here, over source_exporter.py's own independent
    pyodbc connection, since this module is the only place in
    lakebridge_discovery/ that opens one. Each sub-fetch is isolated in
    its own try/except (same permission-sensitive-DMV pattern as
    autovista/sql_metadata_extractor.py) so e.g. a locked-down
    sys.dm_os_sys_info doesn't take down table_features/procedure_parameters."""
    start = time.perf_counter()
    log_entries: list[LakebridgeLogEntry] = []

    def _record(object_type: str, ok: bool, error: str | None = None) -> None:
        log_entries.append(
            LakebridgeLogEntry(
                stage="source_export", object_type=object_type, object_name=object_type,
                status="success" if ok else "failed", error=error,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )
        )

    if config.run_mode == "fixture":
        _populate_fixture_supplementary_metadata(result)
        for object_type in ("server_instance", "table_features", "procedure_parameters", "server_security", "linked_servers"):
            _record(object_type, True)
        return log_entries

    if config.run_mode != "live":
        return log_entries

    try:
        connection = _connect_live_sql(config)
    except Exception as exc:  # noqa: BLE001 - isolate a bad connection from the rest of the run
        error = f"{type(exc).__name__}: {exc}"
        logger.error("FAIL supplementary_metadata connection error=%s", error)
        for object_type in ("server_instance", "table_features", "procedure_parameters", "server_security", "linked_servers"):
            _record(object_type, False, error)
        return log_entries

    try:
        result.server_instance = _fetch_server_instance(connection)
        _record("server_instance", True)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.error("FAIL supplementary_metadata server_instance error=%s", error)
        _record("server_instance", False, error)

    try:
        result.table_features = _fetch_table_features(connection)
        _record("table_features", True)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.error("FAIL supplementary_metadata table_features error=%s", error)
        _record("table_features", False, error)

    try:
        result.procedure_parameters = _fetch_procedure_parameters(connection)
        _record("procedure_parameters", True)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.error("FAIL supplementary_metadata procedure_parameters error=%s", error)
        _record("procedure_parameters", False, error)

    try:
        result.server_principals = _fetch_server_principals(connection)
        result.server_permissions = _fetch_server_permissions(connection)
        _record("server_security", True)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.error("FAIL supplementary_metadata server_security error=%s", error)
        _record("server_security", False, error)

    try:
        result.linked_servers = _fetch_linked_servers(connection)
        _record("linked_servers", True)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        logger.error("FAIL supplementary_metadata linked_servers error=%s", error)
        _record("linked_servers", False, error)

    return log_entries


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
