"""
Database-level metadata: databases, schemas, tables, columns, row counts,
storage size, stored procedures, functions, triggers, SQL Agent jobs.

Row counts and sizes are ALWAYS `direct_metadata` (ground truth from
system catalog views/DMVs) -- never inferred or LLM-estimated, per the
Discovery-phase output contract.

Two `MetadataSource` implementations:
  - LiveSqlServerSource: runs the real queries below against a pyodbc
    connection. This is what a `live` run against an actual SQL Server
    instance uses.
  - FixtureMetadataSource: reads from fixtures/mock_catalog.py. Used only
    for `fixture` run mode (spike/demo/tests) since no live instance is
    reachable in this environment -- never used as a substitute for real
    queries in a live run.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from autovista.logging_setup import log_object_result
from autovista.schema import (
    AgentJobEntity,
    AgentJobStepEntity,
    AssemblyEntity,
    ColumnEntity,
    ConstraintEntity,
    DatabaseEntity,
    DatabaseSummaryEntity,
    FileEntity,
    FunctionEntity,
    IndexEntity,
    ParameterEntity,
    PermissionEntity,
    SequenceEntity,
    SecurityPrincipalEntity,
    StoredProcedureEntity,
    SynonymEntity,
    TableEntity,
    TriggerEntity,
    UserDefinedTypeEntity,
    ViewEntity,
    XmlSchemaCollectionEntity,
)

# --- SQL Agent decode tables (msdb integer codes -> human text) ---
_JOB_RUN_STATUS = {0: "Failed", 1: "Succeeded", 2: "Retry", 3: "Canceled", 4: "In Progress"}
_JOB_STEP_ACTION = {1: "Quit With Success", 2: "Quit With Failure", 3: "Go To Next Step", 4: "Go To Step"}
_JOB_FREQ_TYPE = {
    1: "Once", 4: "Daily", 8: "Weekly", 16: "Monthly", 32: "Monthly Relative",
    64: "Start Automatically When SQL Server Agent Starts", 128: "Start Whenever CPU Is Idle",
}
_NOTIFY_LEVEL = {0: "Never", 1: "Always", 2: "On Failure", 3: "On Success"}


def _decode_int_date(value) -> str | None:
    """msdb packs dates as an int YYYYMMDD (e.g. 20240615, 0 = none)."""
    if not value:
        return None
    s = str(int(value)).zfill(8)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _decode_int_time(value) -> str | None:
    """msdb packs times as an int HHMMSS (e.g. 91530 = 09:15:30)."""
    if value is None:
        return None
    s = str(int(value)).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def _decode_schedule_frequency(freq_type, freq_interval) -> str:
    label = _JOB_FREQ_TYPE.get(freq_type, "Unknown")
    if freq_type in (4, 8, 16) and freq_interval:
        return f"{label} (interval={freq_interval})"
    return label

QUERY_DATABASES = """
SELECT d.name, SUM(mf.size) * 8.0 / 1024 AS size_mb
FROM sys.databases d
JOIN sys.master_files mf ON mf.database_id = d.database_id
WHERE d.name = DB_NAME()
GROUP BY d.name
"""

QUERY_DATABASE_PROPERTIES = """
SELECT d.name, d.recovery_model_desc, d.compatibility_level, SUSER_SNAME(d.owner_sid) AS owner_name,
       d.collation_name, d.create_date, d.is_auto_close_on, d.is_auto_shrink_on,
       d.is_read_only, d.is_trustworthy_on, d.page_verify_option_desc, d.containment_desc,
       d.snapshot_isolation_state_desc, d.is_read_committed_snapshot_on
FROM sys.databases d
WHERE d.name = DB_NAME()
"""

# msdb.dbo.backupset.type: 'D' = full (Database), 'I' = differential, 'L' = log.
QUERY_DATABASE_BACKUPS = """
SELECT MAX(CASE WHEN b.type = 'D' THEN b.backup_finish_date END) AS last_full_backup,
       MAX(CASE WHEN b.type = 'I' THEN b.backup_finish_date END) AS last_differential_backup,
       MAX(CASE WHEN b.type = 'L' THEN b.backup_finish_date END) AS last_log_backup
FROM msdb.dbo.backupset b
WHERE b.database_name = ?
"""

QUERY_DATABASE_LAST_RESTORE = """
SELECT MAX(rh.restore_date) AS last_restore_date
FROM msdb.dbo.restorehistory rh
WHERE rh.destination_database_name = ?
"""

QUERY_DATABASE_FILES = """
SELECT df.name, df.physical_name, fg.name AS filegroup_name, df.size * 8.0 / 1024.0 AS current_size_mb,
       CASE WHEN df.max_size = -1 THEN NULL ELSE df.max_size * 8.0 / 1024.0 END AS max_size_mb,
       CASE WHEN df.is_percent_growth = 1 THEN df.growth ELSE df.growth * 8.0 / 1024.0 END AS growth_value,
       CASE WHEN df.is_percent_growth = 1 THEN 'PERCENT' ELSE 'MB' END AS growth_type
FROM sys.database_files df
LEFT JOIN sys.filegroups fg ON fg.data_space_id = df.data_space_id
"""

QUERY_TABLES = """
SELECT s.name AS schema_name, t.name AS table_name, t.create_date, t.modify_date,
       CASE WHEN EXISTS (SELECT 1 FROM sys.indexes i WHERE i.object_id = t.object_id AND i.type = 0) THEN 'HEAP' ELSE 'CLUSTERED' END AS table_type,
       SUM(ps.row_count) AS row_count,
       SUM(a.total_pages) * 8.0 / 1024 AS size_mb,
       (SELECT COUNT(*) FROM sys.indexes i WHERE i.object_id = t.object_id AND i.type_desc = 'NONCLUSTERED') AS nonclustered_index_count,
       (SELECT COUNT(*) FROM sys.foreign_keys fk WHERE fk.parent_object_id = t.object_id) AS foreign_key_count,
       (SELECT COUNT(*) FROM sys.foreign_keys fk WHERE fk.referenced_object_id = t.object_id) AS referenced_table_count,
       (SELECT COUNT(*) FROM sys.foreign_keys fk WHERE fk.referenced_object_id = t.object_id) AS referencing_table_count,
       (SELECT COUNT(*) FROM sys.triggers tr WHERE tr.parent_id = t.object_id) AS trigger_count,
       SUM(ps.reserved_page_count) AS reserved_pages,
       SUM(ps.used_page_count) AS used_pages,
       SUM(ps.in_row_data_page_count + ps.lob_used_page_count + ps.row_overflow_used_page_count) AS data_pages
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
LEFT JOIN sys.dm_db_partition_stats ps ON ps.object_id = t.object_id AND ps.index_id IN (0, 1)
LEFT JOIN sys.allocation_units a ON a.container_id = ps.partition_id
GROUP BY s.name, t.name, t.create_date, t.modify_date, t.object_id
"""

QUERY_COLUMNS = """
SELECT c.name, ty.name AS data_type, c.is_nullable, c.column_id,
       dc.definition AS default_constraint,
       cc.definition AS check_constraint,
       COLUMNPROPERTY(c.object_id, c.name, 'IsIdentity') AS is_identity,
       CASE WHEN COLUMNPROPERTY(c.object_id, c.name, 'IsIdentity') = 1
            THEN IDENT_SEED(QUOTENAME(OBJECT_SCHEMA_NAME(c.object_id)) + '.' + QUOTENAME(OBJECT_NAME(c.object_id)))
       END AS identity_seed,
       CASE WHEN COLUMNPROPERTY(c.object_id, c.name, 'IsIdentity') = 1
            THEN IDENT_INCR(QUOTENAME(OBJECT_SCHEMA_NAME(c.object_id)) + '.' + QUOTENAME(OBJECT_NAME(c.object_id)))
       END AS identity_increment,
       cmp.definition AS computed_definition,
       COLUMNPROPERTY(c.object_id, c.name, 'IsPersisted') AS is_persisted,
       c.collation_name,
       COLUMNPROPERTY(c.object_id, c.name, 'IsRowGUIDCol') AS is_rowguid,
       c.is_sparse,
       c.is_filestream, ty.is_assembly_type, c.max_length,
       xc.name AS xml_schema_collection_name, SCHEMA_NAME(xc.schema_id) AS xml_schema_collection_schema
FROM sys.columns c
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
LEFT JOIN sys.check_constraints cc ON cc.object_id = c.default_object_id
LEFT JOIN sys.computed_columns cmp ON cmp.object_id = c.object_id AND cmp.column_id = c.column_id
LEFT JOIN sys.xml_schema_collections xc ON xc.xml_collection_id = c.xml_collection_id
WHERE c.object_id = OBJECT_ID(?)
ORDER BY c.column_id
"""

QUERY_PROCEDURES = """
SELECT s.name AS schema_name, p.name AS proc_name, m.definition, p.create_date, p.modify_date,
       OBJECTPROPERTY(p.object_id, 'IsEncrypted') AS is_encrypted, m.execute_as_principal_id, p.object_id
FROM sys.procedures p
JOIN sys.schemas s ON s.schema_id = p.schema_id
JOIN sys.sql_modules m ON m.object_id = p.object_id
"""

QUERY_TRIGGERS = """
SELECT s.name AS schema_name, tr.name AS trigger_name, OBJECT_NAME(tr.parent_id) AS table_name,
       te.type_desc AS event, m.definition
FROM sys.triggers tr
JOIN sys.tables t ON t.object_id = tr.parent_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.sql_modules m ON m.object_id = tr.object_id
CROSS APPLY sys.trigger_events te WHERE te.object_id = tr.object_id
"""

QUERY_AGENT_JOBS = """
SELECT j.job_id, j.name, j.enabled, s.command, j.owner_sid, j.date_created, j.date_modified,
       s.retry_attempts, s.retry_interval, j.description,
       c.name AS category_name, op.name AS notify_operator_name, j.notify_level_email,
       s.step_id, s.step_name, s.subsystem, s.database_name,
       s.on_success_action, s.on_fail_action
FROM msdb.dbo.sysjobs j
LEFT JOIN msdb.dbo.syscategories c ON c.category_id = j.category_id
LEFT JOIN msdb.dbo.sysoperators op ON op.id = j.notify_email_operator_id
JOIN msdb.dbo.sysjobsteps s ON s.job_id = j.job_id
ORDER BY j.name, s.step_id
"""

QUERY_AGENT_JOB_SCHEDULES = """
SELECT js.job_id, sc.name AS schedule_name, sc.freq_type, sc.freq_interval
FROM msdb.dbo.sysjobschedules js
JOIN msdb.dbo.sysschedules sc ON sc.schedule_id = js.schedule_id
"""

QUERY_AGENT_JOB_NEXT_RUN = """
SELECT js.job_id, MIN(js.next_run_date) AS next_run_date, MIN(js.next_run_time) AS next_run_time
FROM msdb.dbo.sysjobschedules js
WHERE js.next_run_date > 0
GROUP BY js.job_id
"""

# instance_id is a monotonically increasing surrogate key on sysjobhistory,
# so MAX(instance_id) per job reliably identifies its most recent run
# (per Microsoft's documented sysjobhistory semantics). step_id = 0 is the
# job-level outcome row, not an individual step's row.
QUERY_AGENT_JOB_LAST_RUN = """
SELECT h.job_id, h.run_date, h.run_time, h.run_status
FROM msdb.dbo.sysjobhistory h
WHERE h.step_id = 0
  AND h.instance_id = (
      SELECT MAX(h2.instance_id) FROM msdb.dbo.sysjobhistory h2
      WHERE h2.job_id = h.job_id AND h2.step_id = 0
  )
"""

QUERY_FOREIGN_KEYS = """
SELECT ps.name AS parent_schema, pt.name AS parent_table, rs.name AS ref_schema, rt.name AS ref_table
FROM sys.foreign_keys fk
JOIN sys.tables pt ON pt.object_id = fk.parent_object_id
JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
"""

QUERY_INDEXES = """
SELECT s.name AS schema_name, t.name AS table_name, i.name AS index_name, i.type_desc, i.is_unique, i.has_filter, i.is_disabled,
       i.fill_factor, i.data_space_id, i.filter_definition, i.object_id, i.index_id, i.is_primary_key
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE i.name IS NOT NULL AND i.type_desc IN ('CLUSTERED','NONCLUSTERED')
ORDER BY s.name, t.name, i.name
"""

# Key vs. included columns, cheap catalog-view lookup (no DMV scan).
QUERY_INDEX_COLUMNS = """
SELECT c.name, ic.is_included_column, ic.is_descending_key
FROM sys.index_columns ic
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE ic.object_id = ? AND ic.index_id = ?
ORDER BY ic.is_included_column, ic.key_ordinal
"""

# Filegroup/partition/allocation-unit are cheap catalog-view joins, one call
# for the whole database (not per-index).
QUERY_INDEX_STORAGE = """
SELECT i.object_id, i.index_id, fg.name AS filegroup_name,
       COUNT(DISTINCT p.partition_number) AS partition_count,
       MAX(au.type_desc) AS allocation_unit_type
FROM sys.indexes i
LEFT JOIN sys.filegroups fg ON fg.data_space_id = i.data_space_id
LEFT JOIN sys.partitions p ON p.object_id = i.object_id AND p.index_id = i.index_id
LEFT JOIN sys.allocation_units au ON au.container_id = p.partition_id
GROUP BY i.object_id, i.index_id, fg.name
"""

# One call for the whole database (not per-index). SAMPLED mode reads a
# sample of an index's pages (or all pages if it has fewer than ~10,000)
# rather than a full scan -- avg_page_space_used_in_percent and record_count
# are only populated in SAMPLED/DETAILED mode (the cheaper LIMITED mode
# only returns fragmentation/page_count), so SAMPLED is the minimum needed
# to answer what this enhancement explicitly asks for. Wrapped by the
# caller in a try/except since this DMV can be restricted by permissions.
QUERY_INDEX_PHYSICAL_STATS = """
SELECT object_id, index_id, avg_fragmentation_in_percent, page_count,
       avg_page_space_used_in_percent, record_count
FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'SAMPLED')
"""

# In-memory usage counters (no scan); reset on service restart, so seeks/
# scans/lookups/updates being NULL for a given index is expected, not an error.
QUERY_INDEX_USAGE_STATS = """
SELECT s.object_id, s.index_id, s.user_seeks, s.user_scans, s.user_lookups, s.user_updates
FROM sys.dm_db_index_usage_stats s
WHERE s.database_id = DB_ID()
"""

# Table-level reserved size (all of a table's indexes + heap combined),
# used as the denominator for an index's percent_of_table. One call for the
# whole database rather than per-table.
QUERY_TABLE_SIZES_BY_OBJECT = """
SELECT ps.object_id, SUM(ps.reserved_page_count) * 8.0 / 1024 AS size_mb
FROM sys.dm_db_partition_stats ps
GROUP BY ps.object_id
"""

# LEFT JOIN sys.sql_modules -- CLR functions (type 'FS'/'FT', not selected
# here anyway since this query is scoped to T-SQL FN/IF/TF) have no module
# row; for the T-SQL function types this query does select, the join
# always matches, but LEFT keeps this defensive rather than assuming it.
QUERY_FUNCTIONS = """
SELECT s.name AS schema_name, f.name AS function_name, f.type_desc, ty.name AS return_type, f.object_id,
       m.definition
FROM sys.objects f
JOIN sys.schemas s ON s.schema_id = f.schema_id
LEFT JOIN sys.parameters p ON p.object_id = f.object_id AND p.parameter_id = 0
LEFT JOIN sys.types ty ON ty.user_type_id = p.user_type_id
LEFT JOIN sys.sql_modules m ON m.object_id = f.object_id
WHERE f.type IN ('FN','IF','TF')
"""

QUERY_SYNONYMS = """
SELECT s.name AS schema_name, sy.name AS synonym_name, sy.base_object_name
FROM sys.synonyms sy
JOIN sys.schemas s ON s.schema_id = sy.schema_id
"""

QUERY_SEQUENCES = """
SELECT s.name AS schema_name, seq.name AS sequence_name, CAST(start_value AS bigint) AS current_value,
       CAST(increment AS bigint) AS increment_value, CAST(minimum_value AS bigint) AS min_value,
       CAST(maximum_value AS bigint) AS max_value, CAST(cache_size AS bigint) AS cache_size
FROM sys.sequences seq
JOIN sys.schemas s ON s.schema_id = seq.schema_id
"""

QUERY_UDTYPES = """
SELECT s.name AS schema_name, t.name AS type_name, t.is_table_type, t.system_type_id, t.user_type_id
FROM sys.types t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_user_defined = 1
"""

QUERY_XML_SCHEMA_COLLECTIONS = """
SELECT s.name AS schema_name, x.name AS collection_name, x.xml_collection_id
FROM sys.xml_schema_collections x
JOIN sys.schemas s ON s.schema_id = x.schema_id
"""

# sys.assemblies has no schema_id -- CLR assemblies are database-scoped, not
# schema-scoped (they were incorrectly joined to sys.schemas by schema_id,
# a column that doesn't exist on sys.assemblies). The closest real,
# catalog-backed equivalent is the owning principal's default schema.
QUERY_ASSEMBLIES = """
SELECT dp.default_schema_name AS schema_name, a.name AS assembly_name, a.permission_set_desc, a.is_visible
FROM sys.assemblies a
LEFT JOIN sys.database_principals dp ON dp.principal_id = a.principal_id
"""

QUERY_SECURITY = """
SELECT dp.name, 'USER' AS principal_type, dp.default_schema_name, dp.owning_principal_id
FROM sys.database_principals dp
WHERE dp.type IN ('U','S','G')
UNION ALL
SELECT dp.name, 'ROLE' AS principal_type, dp.default_schema_name, dp.owning_principal_id
FROM sys.database_principals dp
WHERE dp.type = 'R'
"""

QUERY_PERMISSIONS = """
SELECT dp.name AS grantee_name, dp.type AS principal_type, perm.class_desc, OBJECT_NAME(perm.major_id) AS object_name,
       perm.permission_name, perm.state_desc
FROM sys.database_permissions perm
JOIN sys.database_principals dp ON dp.principal_id = perm.grantee_principal_id
"""

# --- Constraint discovery (Enhancement 2) ---

# Primary keys and unique constraints are both backed by an index
# (sys.key_constraints.type 'PK' or 'UQ'), so their columns come from the
# same index_columns join.
QUERY_PK_UNIQUE_CONSTRAINTS = """
SELECT s.name AS schema_name, t.name AS table_name, kc.name AS constraint_name, kc.type,
       kc.is_system_named, c.name AS column_name, ic.key_ordinal
FROM sys.key_constraints kc
JOIN sys.tables t ON t.object_id = kc.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.indexes i ON i.object_id = kc.parent_object_id AND i.index_id = kc.unique_index_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
ORDER BY s.name, t.name, kc.name, ic.key_ordinal
"""

QUERY_FOREIGN_KEY_CONSTRAINTS = """
SELECT ps.name AS parent_schema, pt.name AS parent_table, fk.name AS constraint_name,
       rs.name AS ref_schema, rt.name AS ref_table,
       pc.name AS parent_column, rc.name AS ref_column, fkc.constraint_column_id,
       fk.delete_referential_action_desc, fk.update_referential_action_desc,
       fk.is_not_trusted, fk.is_disabled, fk.is_system_named
FROM sys.foreign_keys fk
JOIN sys.tables pt ON pt.object_id = fk.parent_object_id
JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
ORDER BY ps.name, pt.name, fk.name, fkc.constraint_column_id
"""

# Column-level CHECK constraints have a non-null parent_column_id; table-level
# ones (referencing multiple columns in the expression) have parent_column_id
# = 0, so column_name comes back NULL via the LEFT JOIN -- expected, not an error.
QUERY_CHECK_CONSTRAINTS = """
SELECT s.name AS schema_name, t.name AS table_name, cc.name AS constraint_name, cc.definition,
       cc.is_disabled, cc.is_not_trusted, cc.is_system_named, col.name AS column_name
FROM sys.check_constraints cc
JOIN sys.tables t ON t.object_id = cc.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
LEFT JOIN sys.columns col ON col.object_id = cc.parent_object_id AND col.column_id = cc.parent_column_id
ORDER BY s.name, t.name, cc.name
"""

QUERY_DEFAULT_CONSTRAINTS = """
SELECT s.name AS schema_name, t.name AS table_name, dc.name AS constraint_name, dc.definition,
       dc.is_system_named, col.name AS column_name
FROM sys.default_constraints dc
JOIN sys.tables t ON t.object_id = dc.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
LEFT JOIN sys.columns col ON col.object_id = dc.parent_object_id AND col.column_id = dc.parent_column_id
ORDER BY s.name, t.name, dc.name
"""

# --- Dependency-discovery metadata backfill (sys.sql_expression_dependencies) ---
# One query for the whole database -- this catalog view is SQL Server's own
# dependency tracker for expression-based references (procs/views/
# functions/triggers/CHECK constraints/computed columns). Used only to fill
# in referenced_tables/procs/functions for objects whose own sqlglot parse
# degraded or failed (see dependency_graph_builder.py's
# _build_metadata_backfill_edges). is_ambiguous rows are excluded by the
# caller, never guessed at here.
QUERY_EXPRESSION_DEPENDENCIES = """
SELECT
    OBJECT_SCHEMA_NAME(sed.referencing_id) AS referencing_schema,
    OBJECT_NAME(sed.referencing_id) AS referencing_name,
    o.type_desc AS referencing_type,
    sed.referenced_schema_name,
    sed.referenced_entity_name,
    sed.referenced_class_desc,
    sed.is_ambiguous
FROM sys.sql_expression_dependencies sed
JOIN sys.objects o ON o.object_id = sed.referencing_id
"""


class MetadataSource(Protocol):
    def list_databases(self) -> list[DatabaseEntity]: ...
    def list_tables(self, database: str) -> list[TableEntity]: ...
    def list_procedures(self, database: str) -> list[tuple[StoredProcedureEntity, str]]: ...  # (entity, definition text)
    def list_triggers(self, database: str) -> list[tuple[TriggerEntity, str]]: ...  # (entity, definition text)
    def list_agent_jobs(self) -> list[AgentJobEntity]: ...
    def list_views(self, database: str) -> list[ViewEntity]: ...
    def list_foreign_keys(self, database: str) -> list[tuple[str, str]]: ...  # (from "schema.table", to "schema.table")
    def list_database_files(self, database: str) -> list[FileEntity]: ...
    def list_indexes(self, database: str) -> list[IndexEntity]: ...
    def list_functions(self, database: str) -> list[tuple[FunctionEntity, str]]: ...  # (entity, definition text)
    def list_synonyms(self, database: str) -> list[SynonymEntity]: ...
    def list_sequences(self, database: str) -> list[SequenceEntity]: ...
    def list_user_defined_types(self, database: str) -> list[UserDefinedTypeEntity]: ...
    def list_xml_schema_collections(self, database: str) -> list[XmlSchemaCollectionEntity]: ...
    def list_assemblies(self, database: str) -> list[AssemblyEntity]: ...
    def list_security_principals(self, database: str) -> list[SecurityPrincipalEntity]: ...
    def list_permissions(self, database: str) -> list[PermissionEntity]: ...
    def list_database_summary(self, database: str) -> list[DatabaseSummaryEntity]: ...
    def list_constraints(self, database: str) -> list[ConstraintEntity]: ...
    def list_expression_dependencies(self, database: str) -> list[tuple[str, str, str, str, str]]: ...
    # (referencing_schema, referencing_name, referencing_type, referenced_schema, referenced_name)


@dataclass
class LiveSqlServerSource:
    """Queries a real SQL Server instance via pyodbc. Requires a
    read-only service account -- credentials come from config.SqlServerConfig,
    never hardcoded here."""

    connection: "object"

    def _use_database(self, database: str) -> None:
        self.connection.cursor().execute(f"USE [{database}]")

    def _format_datetime(self, value):
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    def _fetch_backup_restore_dates(self, database: str):
        """Returns (last_full, last_differential, last_log, last_restore),
        each a raw datetime or None. Wrapped by the caller in a try/except --
        the read-only discovery account is only guaranteed db_datareader on
        SSISDB + source databases (see README), not necessarily msdb, so a
        permissions error here must not take down database extraction."""
        last_full = last_diff = last_log = last_restore = None
        cur = self.connection.cursor()
        cur.execute(QUERY_DATABASE_BACKUPS, database)
        row = cur.fetchone()
        if row:
            last_full, last_diff, last_log = row
        cur = self.connection.cursor()
        cur.execute(QUERY_DATABASE_LAST_RESTORE, database)
        row = cur.fetchone()
        if row:
            last_restore = row[0]
        return last_full, last_diff, last_log, last_restore

    def _populate_database_properties(self, entity: DatabaseEntity) -> None:
        cur = self.connection.cursor()
        cur.execute(QUERY_DATABASE_PROPERTIES)
        row = cur.fetchone()
        if row:
            (_, recovery_model_desc, compatibility_level, owner_name, collation_name, create_date,
             is_auto_close_on, is_auto_shrink_on, is_read_only, is_trustworthy_on,
             page_verify_option_desc, containment_desc, snapshot_isolation_state_desc,
             is_read_committed_snapshot_on) = row
            entity.recovery_model = recovery_model_desc
            entity.compatibility_level = str(compatibility_level) if compatibility_level is not None else None
            entity.database_owner = owner_name
            entity.collation_name = collation_name
            entity.create_date = self._format_datetime(create_date)
            entity.auto_close = bool(is_auto_close_on) if is_auto_close_on is not None else None
            entity.auto_shrink = bool(is_auto_shrink_on) if is_auto_shrink_on is not None else None
            entity.is_read_only = bool(is_read_only) if is_read_only is not None else None
            entity.is_trustworthy_on = bool(is_trustworthy_on) if is_trustworthy_on is not None else None
            entity.page_verify_option = page_verify_option_desc
            entity.containment = containment_desc
            entity.is_snapshot_isolation_on = (
                snapshot_isolation_state_desc == "ON" if snapshot_isolation_state_desc is not None else None
            )
            entity.is_read_committed_snapshot_on = (
                bool(is_read_committed_snapshot_on) if is_read_committed_snapshot_on is not None else None
            )

        try:
            last_full, last_diff, last_log, last_restore = self._fetch_backup_restore_dates(entity.name)
            entity.last_full_backup = self._format_datetime(last_full)
            entity.last_differential_backup = self._format_datetime(last_diff)
            entity.last_log_backup = self._format_datetime(last_log)
            entity.last_restore_date = self._format_datetime(last_restore)
            latest = max((d for d in (last_full, last_diff, last_log) if d is not None), default=None)
            entity.last_backup_date = self._format_datetime(latest)
        except Exception:
            pass  # msdb backup/restore history unavailable -- leave fields at their defaults (None)

    def list_databases(self) -> list[DatabaseEntity]:
        cur = self.connection.cursor()
        cur.execute(QUERY_DATABASES)
        rows = cur.fetchall()
        out = []
        for name, size_mb in rows:
            entity = DatabaseEntity(name=name, size_mb=round(float(size_mb), 2), table_count=0, proc_count=0, view_count=0)
            self._populate_database_properties(entity)
            out.append(entity)
        return out

    def list_database_files(self, database: str) -> list[FileEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_DATABASE_FILES)
        rows = list(cur.fetchall())
        total_size = sum(float(r[3] or 0) for r in rows)
        out = []
        for logical_name, physical_name, filegroup_name, current_size_mb, max_size_mb, growth_value, growth_type in rows:
            percent = round((float(current_size_mb or 0) / total_size * 100.0), 2) if total_size else None
            out.append(
                FileEntity(
                    database=database,
                    logical_name=logical_name,
                    physical_name=physical_name,
                    filegroup=filegroup_name,
                    current_size_mb=round(float(current_size_mb or 0), 2),
                    max_size_mb=round(float(max_size_mb or 0), 2) if max_size_mb is not None else None,
                    growth_mb=round(float(growth_value or 0), 2),
                    growth_type=growth_type,
                    percent_of_total_database=percent,
                )
            )
        return out

    def list_tables(self, database: str) -> list[TableEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_DATABASES)
        size_row = cur.fetchone()
        total_database_size_mb = float(size_row[1]) if size_row else 0.0

        cur = self.connection.cursor()
        cur.execute(QUERY_TABLES)
        tables = []
        for schema_name, table_name, create_date, modify_date, table_type, row_count, size_mb, nonclustered_index_count, foreign_key_count, referenced_table_count, referencing_table_count, trigger_count, reserved_pages, used_pages, data_pages in cur.fetchall():
            col_cur = self.connection.cursor()
            col_cur.execute(QUERY_COLUMNS, f"[{schema_name}].[{table_name}]")
            columns = []
            for c in col_cur.fetchall():
                columns.append(
                    ColumnEntity(
                        name=c[0],
                        data_type=c[1],
                        nullable=bool(c[2]),
                        ordinal_position=c[3],
                        default_constraint=c[4],
                        check_constraint=c[5],
                        # c[6] is is_identity (existence flag only -- no dedicated
                        # ColumnEntity field for it; identity-ness is implied by
                        # identity_seed/identity_increment being non-null).
                        identity_seed=int(c[7]) if c[7] is not None else None,
                        identity_increment=int(c[8]) if c[8] is not None else None,
                        computed_expression=None if c[9] is None else str(c[9]),
                        is_persisted=bool(c[10]) if c[10] is not None else None,
                        collation_name=c[11],
                        is_rowguid=bool(c[12]) if c[12] is not None else None,
                        is_sparse=bool(c[13]) if c[13] is not None else None,
                        is_filestream=bool(c[14]) if c[14] is not None else None,
                        is_clr_type=bool(c[15]) if c[15] is not None else None,
                        max_length=int(c[16]) if c[16] is not None else None,
                        xml_schema_collection=(
                            f"{c[18]}.{c[17]}" if c[17] is not None and c[18] is not None else None
                        ),
                    )
                )
            tables.append(
                TableEntity(
                    database=database,
                    schema=schema_name,
                    name=table_name,
                    row_count=int(row_count or 0),
                    size_mb=round(float(size_mb or 0), 2),
                    column_count=len(columns),
                    columns=columns,
                    create_date=self._format_datetime(create_date),
                    modify_date=self._format_datetime(modify_date),
                    table_type=table_type,
                    nonclustered_index_count=int(nonclustered_index_count or 0),
                    foreign_key_count=int(foreign_key_count or 0),
                    referenced_table_count=int(referenced_table_count or 0),
                    referencing_table_count=int(referencing_table_count or 0),
                    trigger_count=int(trigger_count or 0),
                    estimated_reserved_pages=int(reserved_pages or 0),
                    used_pages=int(used_pages or 0),
                    data_pages=int(data_pages or 0),
                    percent_of_database_occupied=(
                        round(float(size_mb or 0) / total_database_size_mb * 100.0, 2)
                        if total_database_size_mb else None
                    ),
                    # Derived from the columns already fetched above -- no extra query.
                    # These table-level convenience lists were previously never
                    # populated for live tables (only per-column flags were set).
                    identity_columns=[c.name for c in columns if c.identity_seed is not None],
                    computed_columns=[c.name for c in columns if c.computed_expression is not None],
                    sparse_columns=[c.name for c in columns if c.is_sparse],
                )
            )
        tables.sort(key=lambda t: t.size_mb, reverse=True)
        return tables

    def list_procedures(self, database: str) -> list[tuple[StoredProcedureEntity, str]]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_PROCEDURES)
        return [
            (
                StoredProcedureEntity(
                    database=database,
                    schema=schema_name,
                    name=proc_name,
                    loc=len(definition.splitlines()),
                    create_date=self._format_datetime(create_date),
                    modify_date=self._format_datetime(modify_date),
                    is_encrypted=bool(is_encrypted),
                    execute_as=repr(execute_as_principal_id),
                    parameter_count=0,
                    parse_status="direct_metadata",
                ),
                definition,
            )
            for schema_name, proc_name, definition, create_date, modify_date, is_encrypted, execute_as_principal_id, _ in cur.fetchall()
        ]

    def list_triggers(self, database: str) -> list[tuple[TriggerEntity, str]]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_TRIGGERS)
        return [
            (TriggerEntity(database=database, schema=s, name=n, table=t, event=e), definition)
            for s, n, t, e, definition in cur.fetchall()
        ]

    def list_agent_jobs(self) -> list[AgentJobEntity]:
        cur = self.connection.cursor()
        cur.execute(QUERY_AGENT_JOBS)
        jobs: dict[str, AgentJobEntity] = {}
        job_ids_by_name: dict[str, int] = {}
        for (job_id, name, enabled, command, owner_sid, date_created, date_modified, retry_attempts,
             retry_interval, description, category_name, notify_operator_name, notify_level_email,
             step_id, step_name, subsystem, database_name, on_success_action, on_fail_action) in cur.fetchall():
            job = jobs.setdefault(name, AgentJobEntity(name=name, enabled=bool(enabled), owner=str(owner_sid), schedule=description))
            job.steps.append(command)
            # Preserved exactly as before (Enhancement 1 is additive-only) --
            # these were already mapped to date_created/date_modified rather
            # than true run dates; see last_run_date/next_scheduled_run for
            # the correct values sourced from sysjobhistory/sysjobschedules.
            job.last_run=self._format_datetime(date_created)
            job.next_run=self._format_datetime(date_modified)
            job.retry_attempts=int(retry_attempts or 0)
            job.retry_interval=int(retry_interval or 0)

            job.category = category_name
            job.description = description
            job.date_created = self._format_datetime(date_created)
            job.date_modified = self._format_datetime(date_modified)
            job.notification_operator = notify_operator_name
            job.notification_method = _NOTIFY_LEVEL.get(notify_level_email)

            job.step_details.append(
                AgentJobStepEntity(
                    step_id=step_id,
                    name=step_name,
                    subsystem=subsystem,
                    database_name=database_name,
                    command=command,
                    on_success_action=_JOB_STEP_ACTION.get(on_success_action),
                    on_fail_action=_JOB_STEP_ACTION.get(on_fail_action),
                    retry_attempts=int(retry_attempts) if retry_attempts is not None else None,
                    retry_interval=int(retry_interval) if retry_interval is not None else None,
                )
            )
            job.step_count = len(job.step_details)
            job_ids_by_name[name] = job_id

        try:
            self._attach_agent_job_schedules(jobs, job_ids_by_name)
        except Exception:
            pass  # sysjobschedules/sysschedules unavailable -- leave schedule/next-run fields empty

        try:
            self._attach_agent_job_last_run(jobs, job_ids_by_name)
        except Exception:
            pass  # sysjobhistory unavailable -- leave last_run_date/status empty

        return list(jobs.values())

    def _attach_agent_job_schedules(self, jobs: dict, job_ids_by_name: dict) -> None:
        id_to_name = {v: k for k, v in job_ids_by_name.items()}

        cur = self.connection.cursor()
        cur.execute(QUERY_AGENT_JOB_SCHEDULES)
        for job_id, schedule_name, freq_type, freq_interval in cur.fetchall():
            job = jobs.get(id_to_name.get(job_id))
            if job is None:
                continue
            job.schedule_names.append(schedule_name)
            job.schedule_frequency.append(_decode_schedule_frequency(freq_type, freq_interval))

        cur = self.connection.cursor()
        cur.execute(QUERY_AGENT_JOB_NEXT_RUN)
        for job_id, next_run_date, next_run_time in cur.fetchall():
            job = jobs.get(id_to_name.get(job_id))
            if job is None:
                continue
            date_part = _decode_int_date(next_run_date)
            if date_part:
                job.next_scheduled_run = f"{date_part}T{_decode_int_time(next_run_time) or '00:00:00'}"

    def _attach_agent_job_last_run(self, jobs: dict, job_ids_by_name: dict) -> None:
        id_to_name = {v: k for k, v in job_ids_by_name.items()}

        cur = self.connection.cursor()
        cur.execute(QUERY_AGENT_JOB_LAST_RUN)
        for job_id, run_date, run_time, run_status in cur.fetchall():
            job = jobs.get(id_to_name.get(job_id))
            if job is None:
                continue
            job.last_run_date = _decode_int_date(run_date)
            job.last_run_time = _decode_int_time(run_time)
            status_text = _JOB_RUN_STATUS.get(run_status)
            job.last_run_status = status_text
            job.last_outcome = status_text  # existing field, previously never populated

    def list_views(self, database: str) -> list[ViewEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(
            """
            SELECT s.name, v.name, m.definition, v.create_date, v.modify_date
            FROM sys.views v
            JOIN sys.schemas s ON s.schema_id = v.schema_id
            JOIN sys.sql_modules m ON m.object_id = v.object_id
            """
        )
        return [
            ViewEntity(
                database=database,
                schema=schema_name,
                name=view_name,
                create_date=self._format_datetime(create_date),
                modify_date=self._format_datetime(modify_date),
                parse_status="direct_metadata",
            )
            for schema_name, view_name, _, create_date, modify_date in cur.fetchall()
        ]

    def list_foreign_keys(self, database: str) -> list[tuple[str, str]]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_FOREIGN_KEYS)
        return [(f"{ps}.{pt}", f"{rs}.{rt}") for ps, pt, rs, rt in cur.fetchall()]

    def list_indexes(self, database: str) -> list[IndexEntity]:
        self._use_database(database)

        cur = self.connection.cursor()
        cur.execute(QUERY_DATABASES)
        size_row = cur.fetchone()
        total_database_size_mb = float(size_row[1]) if size_row else 0.0

        cur = self.connection.cursor()
        cur.execute(QUERY_TABLE_SIZES_BY_OBJECT)
        table_size_by_object_id = {object_id: float(size_mb or 0) for object_id, size_mb in cur.fetchall()}

        storage_by_index: dict = {}
        try:
            cur = self.connection.cursor()
            cur.execute(QUERY_INDEX_STORAGE)
            for object_id, index_id, filegroup_name, partition_count, allocation_unit_type in cur.fetchall():
                storage_by_index[(object_id, index_id)] = (filegroup_name, partition_count, allocation_unit_type)
        except Exception:
            pass  # storage breakdown unavailable -- filegroup/partition_count/allocation_unit_type stay None

        physical_by_index: dict = {}
        try:
            cur = self.connection.cursor()
            cur.execute(QUERY_INDEX_PHYSICAL_STATS)
            for object_id, index_id, frag_pct, page_count, avg_page_space_used_pct, record_count in cur.fetchall():
                physical_by_index[(object_id, index_id)] = (frag_pct, page_count, avg_page_space_used_pct, record_count)
        except Exception:
            pass  # dm_db_index_physical_stats unavailable (permissions/engine edition) -- operational stats stay None

        usage_by_index: dict = {}
        try:
            cur = self.connection.cursor()
            cur.execute(QUERY_INDEX_USAGE_STATS)
            for object_id, index_id, seeks, scans, lookups, updates in cur.fetchall():
                usage_by_index[(object_id, index_id)] = (seeks, scans, lookups, updates)
        except Exception:
            pass  # usage stats unavailable -- user_seeks/scans/lookups/updates stay None

        cur = self.connection.cursor()
        cur.execute(QUERY_INDEXES)
        rows = cur.fetchall()
        out = []
        for (schema_name, table_name, index_name, type_desc, is_unique, has_filter, is_disabled, fill_factor,
             _, filter_definition, object_id, index_id, is_primary_key) in rows:
            col_cur = self.connection.cursor()
            col_cur.execute(QUERY_INDEX_COLUMNS, object_id, index_id)
            key_columns = []
            key_column_sort = []
            included_columns = []
            for col_name, is_included, is_descending in col_cur.fetchall():
                if is_included:
                    included_columns.append(col_name)
                else:
                    key_columns.append(col_name)
                    key_column_sort.append("DESC" if is_descending else "ASC")

            filegroup_name, partition_count, allocation_unit_type = storage_by_index.get((object_id, index_id), (None, None, None))
            frag_pct, page_count, avg_page_space_used_pct, record_count = physical_by_index.get((object_id, index_id), (None, None, None, None))
            seeks, scans, lookups, updates = usage_by_index.get((object_id, index_id), (None, None, None, None))

            index_size_mb = round(float(page_count) * 8.0 / 1024, 2) if page_count is not None else None
            table_size_mb = table_size_by_object_id.get(object_id)
            percent_of_table = round(index_size_mb / table_size_mb * 100.0, 2) if index_size_mb and table_size_mb else None
            percent_of_database = round(index_size_mb / total_database_size_mb * 100.0, 2) if index_size_mb and total_database_size_mb else None

            out.append(
                IndexEntity(
                    database=database,
                    schema=schema_name,
                    table=table_name,
                    name=index_name,
                    is_clustered=type_desc == 'CLUSTERED',
                    is_nonclustered=type_desc == 'NONCLUSTERED',
                    is_unique=bool(is_unique),
                    is_filtered=bool(has_filter),
                    is_disabled=bool(is_disabled),
                    fill_factor=fill_factor,
                    compression=None,
                    fragmentation_pct=round(float(frag_pct), 2) if frag_pct is not None else None,
                    page_count=int(page_count) if page_count is not None else None,
                    index_size_mb=index_size_mb,
                    key_columns=key_columns,
                    included_columns=included_columns,
                    index_type=type_desc,
                    is_primary_key=bool(is_primary_key),
                    filter_definition=filter_definition,
                    key_column_sort=key_column_sort,
                    is_partitioned=bool(partition_count and partition_count > 1),
                    partition_count=partition_count,
                    filegroup=filegroup_name,
                    allocation_unit_type=allocation_unit_type,
                    user_seeks=int(seeks) if seeks is not None else None,
                    user_scans=int(scans) if scans is not None else None,
                    user_lookups=int(lookups) if lookups is not None else None,
                    user_updates=int(updates) if updates is not None else None,
                    avg_page_space_used_pct=round(float(avg_page_space_used_pct), 2) if avg_page_space_used_pct is not None else None,
                    record_count=int(record_count) if record_count is not None else None,
                    percent_of_table=percent_of_table,
                    percent_of_database=percent_of_database,
                )
            )
        return out

    def list_functions(self, database: str) -> list[tuple[FunctionEntity, str]]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_FUNCTIONS)
        return [
            (
                FunctionEntity(database=database, schema=schema_name, name=function_name, function_type=function_type or 'SCALAR', return_type=return_type),
                definition or "",
            )
            for schema_name, function_name, function_type, return_type, _, definition in cur.fetchall()
        ]

    def list_synonyms(self, database: str) -> list[SynonymEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_SYNONYMS)
        return [
            SynonymEntity(database=database, schema=schema_name, name=synonym_name, base_object=base_object_name)
            for schema_name, synonym_name, base_object_name in cur.fetchall()
        ]

    def list_sequences(self, database: str) -> list[SequenceEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_SEQUENCES)
        return [
            SequenceEntity(database=database, schema=schema_name, name=sequence_name, current_value=current_value, increment=increment_value, minimum_value=min_value, maximum_value=max_value, cache=cache_size)
            for schema_name, sequence_name, current_value, increment_value, min_value, max_value, cache_size in cur.fetchall()
        ]

    def list_user_defined_types(self, database: str) -> list[UserDefinedTypeEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_UDTYPES)
        return [
            UserDefinedTypeEntity(database=database, schema=schema_name, name=type_name, type_kind='TABLE_TYPE' if is_table_type else 'USER_DEFINED', base_type=str(user_type_id))
            for schema_name, type_name, is_table_type, _, user_type_id in cur.fetchall()
        ]

    def list_xml_schema_collections(self, database: str) -> list[XmlSchemaCollectionEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_XML_SCHEMA_COLLECTIONS)
        return [
            XmlSchemaCollectionEntity(database=database, schema=schema_name, name=collection_name)
            for schema_name, collection_name, _ in cur.fetchall()
        ]

    def list_assemblies(self, database: str) -> list[AssemblyEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_ASSEMBLIES)
        return [
            AssemblyEntity(database=database, schema=schema_name, name=assembly_name, permission_set=permission_set_desc, is_visible=is_visible)
            for schema_name, assembly_name, permission_set_desc, is_visible in cur.fetchall()
        ]

    def list_security_principals(self, database: str) -> list[SecurityPrincipalEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_SECURITY)
        return [
            SecurityPrincipalEntity(database=database, name=name, principal_type=principal_type, default_schema=default_schema_name, owning_principal=str(owning_principal_id))
            for name, principal_type, default_schema_name, owning_principal_id in cur.fetchall()
        ]

    def list_permissions(self, database: str) -> list[PermissionEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_PERMISSIONS)
        return [
            PermissionEntity(database=database, grantee=grantee_name, principal_type=principal_type, class_desc=class_desc, object_name=object_name, permission_name=permission_name, state_desc=state_desc)
            for grantee_name, principal_type, class_desc, object_name, permission_name, state_desc in cur.fetchall()
        ]

    def list_database_summary(self, database: str) -> list[DatabaseSummaryEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_DATABASE_PROPERTIES)
        row = cur.fetchone()
        if not row:
            return []

        last_backup = last_restore = None
        try:
            last_full, last_diff, last_log, last_restore_raw = self._fetch_backup_restore_dates(database)
            last_backup = max((d for d in (last_full, last_diff, last_log) if d is not None), default=None)
            last_backup = self._format_datetime(last_backup)
            last_restore = self._format_datetime(last_restore_raw)
        except Exception:
            pass  # msdb backup/restore history unavailable -- leave as None

        cur = self.connection.cursor()
        cur.execute(QUERY_DATABASES)
        size_row = cur.fetchone()
        database_size_mb = round(float(size_row[1]), 2) if size_row else 0.0

        return [
            DatabaseSummaryEntity(
                database=database,
                recovery_model=row[1],
                compatibility_level=str(row[2]) if row[2] is not None else None,
                last_backup=last_backup,
                last_restore=last_restore,
                database_size_mb=database_size_mb,
            )
        ]

    def list_constraints(self, database: str) -> list[ConstraintEntity]:
        self._use_database(database)
        constraints: list[ConstraintEntity] = []

        cur = self.connection.cursor()
        cur.execute(QUERY_PK_UNIQUE_CONSTRAINTS)
        pk_unique: dict[tuple, ConstraintEntity] = {}
        for schema_name, table_name, constraint_name, kc_type, is_system_named, column_name, _ in cur.fetchall():
            key = (schema_name, table_name, constraint_name)
            entity = pk_unique.setdefault(key, ConstraintEntity(
                database=database, schema=schema_name, table=table_name, name=constraint_name,
                constraint_type="PRIMARY_KEY" if kc_type == "PK" else "UNIQUE",
                is_system_named=bool(is_system_named),
            ))
            entity.columns.append(column_name)
        constraints.extend(pk_unique.values())

        cur = self.connection.cursor()
        cur.execute(QUERY_FOREIGN_KEY_CONSTRAINTS)
        fks: dict[tuple, ConstraintEntity] = {}
        for (parent_schema, parent_table, constraint_name, ref_schema, ref_table, parent_column, ref_column,
             _, delete_action, update_action, is_not_trusted, is_disabled, is_system_named) in cur.fetchall():
            key = (parent_schema, parent_table, constraint_name)
            entity = fks.setdefault(key, ConstraintEntity(
                database=database, schema=parent_schema, table=parent_table, name=constraint_name,
                constraint_type="FOREIGN_KEY",
                referenced_table=f"{ref_schema}.{ref_table}",
                delete_action=delete_action, update_action=update_action,
                is_trusted=not bool(is_not_trusted), is_disabled=bool(is_disabled),
                is_system_named=bool(is_system_named),
            ))
            entity.columns.append(parent_column)
            entity.referenced_columns.append(ref_column)
        constraints.extend(fks.values())

        cur = self.connection.cursor()
        cur.execute(QUERY_CHECK_CONSTRAINTS)
        for schema_name, table_name, constraint_name, definition, is_disabled, is_not_trusted, is_system_named, column_name in cur.fetchall():
            constraints.append(ConstraintEntity(
                database=database, schema=schema_name, table=table_name, name=constraint_name,
                constraint_type="CHECK",
                columns=[column_name] if column_name else [],
                is_trusted=not bool(is_not_trusted), is_disabled=bool(is_disabled),
                is_system_named=bool(is_system_named), definition=definition,
            ))

        cur = self.connection.cursor()
        cur.execute(QUERY_DEFAULT_CONSTRAINTS)
        for schema_name, table_name, constraint_name, definition, is_system_named, column_name in cur.fetchall():
            constraints.append(ConstraintEntity(
                database=database, schema=schema_name, table=table_name, name=constraint_name,
                constraint_type="DEFAULT",
                columns=[column_name] if column_name else [],
                is_system_named=bool(is_system_named), definition=definition,
            ))

        return constraints

    def list_expression_dependencies(self, database: str) -> list[tuple[str, str, str, str, str]]:
        """sys.sql_expression_dependencies, whole-database in one query --
        see QUERY_EXPRESSION_DEPENDENCIES. is_ambiguous rows and non-object
        references (referenced_class_desc != 'OBJECT_OR_COLUMN', e.g. TYPE/
        XML_NAMESPACE) are filtered out here rather than left for the
        caller, since neither is safely usable as a dependency target --
        the caller (dependency_graph_builder.py) still separately decides
        WHICH of these rows apply, by only matching against objects whose
        own sqlglot parse degraded or failed."""
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_EXPRESSION_DEPENDENCIES)
        rows = []
        for (ref_schema, ref_name, ref_type, target_schema, target_name, target_class, is_ambiguous) in cur.fetchall():
            if is_ambiguous or target_class != "OBJECT_OR_COLUMN" or not ref_schema or not ref_name or not target_name:
                continue
            rows.append((ref_schema, ref_name, ref_type, target_schema, target_name))
        return rows


@dataclass
class FixtureMetadataSource:
    """Fixture-mode source backed by fixtures/mock_catalog.py. Demo/test
    only -- see module docstring."""

    catalog: "object"

    def list_databases(self) -> list[DatabaseEntity]:
        c = self.catalog
        return [
            DatabaseEntity(
                name=c.database_name,
                size_mb=c.database_size_mb(),
                table_count=len(c.tables),
                proc_count=len(c.procedures),
                view_count=len(c.views),
                data_file_size_mb=c.data_file_size_mb(),
                log_file_size_mb=c.log_file_size_mb(),
                data_occupied_pct=c.data_occupied_pct(),
                log_occupied_pct=c.log_occupied_pct(),
                recovery_model="FULL",
                compatibility_level="SQL Server 2022",
                database_owner="dbo",
                collation_name="SQL_Latin1_General_CP1_CI_AS",
                create_date="2024-01-01T00:00:00",
                last_backup_date="2024-06-15T00:00:00",
                last_full_backup="2024-06-15T00:00:00",
                last_differential_backup="2024-06-14T00:00:00",
                last_log_backup="2024-06-15T01:00:00",
                last_restore_date="2024-06-16T00:00:00",
                auto_close=False,
                auto_shrink=False,
                is_read_only=False,
                is_trustworthy_on=True,
                page_verify_option="CHECKSUM",
                containment="NONE",
                is_snapshot_isolation_on=False,
                is_read_committed_snapshot_on=True,
            )
        ]

    def list_database_files(self, database: str) -> list[FileEntity]:
        return [
            FileEntity(database=database, logical_name="SalesDW_Data", physical_name="C:/Data/SalesDW.mdf", filegroup="PRIMARY", current_size_mb=2048.0, max_size_mb=None, growth_mb=512.0, growth_type="MB", percent_of_total_database=80.0),
            FileEntity(database=database, logical_name="SalesDW_Log", physical_name="C:/Log/SalesDW.ldf", filegroup=None, current_size_mb=512.0, max_size_mb=None, growth_mb=128.0, growth_type="MB", percent_of_total_database=20.0),
        ]

    def list_tables(self, database: str) -> list[TableEntity]:
        c = self.catalog
        out = []
        for key, t in c.tables.items():
            columns = [
                ColumnEntity(name=col.name, data_type=col.data_type, nullable=col.nullable, ordinal_position=col.ordinal_position, identity_seed=1 if col.name == "OrderId" else None, identity_increment=1 if col.name == "OrderId" else None, is_part_of_pk=col.name == "OrderId", is_nullable=(False if col.name == "OrderId" else col.nullable))
                for col in t.columns
            ]
            out.append(
                TableEntity(
                    database=database,
                    schema=t.schema,
                    name=t.name,
                    row_count=c.row_count(t.schema, t.name),
                    size_mb=c.size_mb(t.schema, t.name),
                    column_count=len(t.columns),
                    columns=columns,
                    create_date="2024-01-01T00:00:00",
                    modify_date="2024-06-15T00:00:00",
                    table_type="CLUSTERED",
                    index_count=2,
                    nonclustered_index_count=1,
                    foreign_key_count=1,
                    referenced_table_count=1,
                    referencing_table_count=1,
                    trigger_count=1,
                    identity_columns=[col.name for col in t.columns if col.name == "OrderId"],
                    computed_columns=[],
                    sparse_columns=[],
                    rowguid_columns=[],
                    lob_columns=[],
                    is_temporal_table=False,
                    is_memory_optimized=False,
                    is_cdc_enabled=False,
                    is_change_tracking_enabled=False,
                    is_partitioned=False,
                    partition_count=1,
                    estimated_reserved_pages=100,
                    used_pages=80,
                    data_pages=70,
                    percent_of_database_occupied=round(c.size_mb(t.schema, t.name) / max(c.database_size_mb(), 1) * 100.0, 2),
                )
            )
        return out

    def list_procedures(self, database: str) -> list[tuple[StoredProcedureEntity, str]]:
        c = self.catalog
        return [
            (
                StoredProcedureEntity(
                    database=database,
                    schema=p.schema,
                    name=p.name,
                    loc=len(p.definition.splitlines()),
                    create_date="2024-01-01T00:00:00",
                    modify_date="2024-06-15T00:00:00",
                    is_encrypted=False,
                    execute_as="dbo",
                    parameter_count=2,
                    dynamic_sql_usage=False,
                    parse_status="direct_metadata",
                ),
                p.definition,
            )
            for p in c.procedures.values()
        ]

    def list_triggers(self, database: str) -> list[tuple[TriggerEntity, str]]:
        c = self.catalog
        return [
            (
                TriggerEntity(database=database, schema=t.schema, name=t.name, table=t.table, event=t.event),
                t.definition,
            )
            for t in c.triggers
        ]

    def list_agent_jobs(self) -> list[AgentJobEntity]:
        return [AgentJobEntity(**job) for job in self.catalog.agent_jobs()]

    def list_views(self, database: str) -> list[ViewEntity]:
        return [
            ViewEntity(database=database, schema=v.schema, name=v.name, create_date="2024-01-01T00:00:00", modify_date="2024-06-15T00:00:00", parse_status="direct_metadata")
            for v in self.catalog.views.values()
        ]

    def list_foreign_keys(self, database: str) -> list[tuple[str, str]]:
        return list(self.catalog.foreign_keys)

    def list_indexes(self, database: str) -> list[IndexEntity]:
        return [
            IndexEntity(
                database=database, schema="dbo", table="Orders", name="IX_Orders_CustomerId",
                is_clustered=False, is_nonclustered=True, is_unique=False, is_filtered=False, is_disabled=False,
                fill_factor=90, compression="NONE", fragmentation_pct=2.5, page_count=12, index_size_mb=0.09,
                key_columns=["CustomerId"], key_column_sort=["ASC"], included_columns=["OrderDate"],
                index_type="NONCLUSTERED", is_primary_key=False, is_partitioned=False, partition_count=1,
                filegroup="PRIMARY", allocation_unit_type="IN_ROW_DATA",
                user_seeks=120, user_scans=4, user_lookups=0, user_updates=15,
                avg_page_space_used_pct=78.4, record_count=1000,
                percent_of_table=1.2, percent_of_database=0.03,
            )
        ]

    def list_functions(self, database: str) -> list[tuple[FunctionEntity, str]]:
        # Two functions, one calling the other and reading a table, so
        # fixture mode demonstrates Function->Table and Function->Function
        # detection without needing real live SQL Server metadata.
        return [
            (
                FunctionEntity(database=database, schema="dbo", name="ufn_GetOrderStatus", function_type="SCALAR"),
                "CREATE FUNCTION dbo.ufn_GetOrderStatus(@OrderId INT) RETURNS NVARCHAR(20) AS "
                "BEGIN RETURN (SELECT dbo.ufn_GetOrderStatusLabel(o.OrderId) FROM dbo.Orders o WHERE o.OrderId = @OrderId) END",
            ),
            (
                FunctionEntity(database=database, schema="dbo", name="ufn_GetOrderStatusLabel", function_type="SCALAR"),
                "CREATE FUNCTION dbo.ufn_GetOrderStatusLabel(@OrderId INT) RETURNS NVARCHAR(20) AS "
                "BEGIN RETURN 'Open' END",
            ),
        ]

    def list_synonyms(self, database: str) -> list[SynonymEntity]:
        return [SynonymEntity(database=database, schema="dbo", name="CustomerAlias", base_object="dbo.Customers")]

    def list_sequences(self, database: str) -> list[SequenceEntity]:
        return [SequenceEntity(database=database, schema="dbo", name="Seq_OrderId", current_value=1000, increment=1, minimum_value=1, maximum_value=2147483647, cache=50)]

    def list_user_defined_types(self, database: str) -> list[UserDefinedTypeEntity]:
        return [UserDefinedTypeEntity(database=database, schema="dbo", name="PhoneNumber", type_kind="ALIAS", base_type="nvarchar(20)")]

    def list_xml_schema_collections(self, database: str) -> list[XmlSchemaCollectionEntity]:
        return [XmlSchemaCollectionEntity(database=database, schema="dbo", name="OrderSchema")]

    def list_assemblies(self, database: str) -> list[AssemblyEntity]:
        return [AssemblyEntity(database=database, schema="dbo", name="SalesDWCLR", permission_set="SAFE", is_visible=True)]

    def list_security_principals(self, database: str) -> list[SecurityPrincipalEntity]:
        return [SecurityPrincipalEntity(database=database, name="dbo", principal_type="USER"), SecurityPrincipalEntity(database=database, name="db_datareader", principal_type="ROLE")]

    def list_permissions(self, database: str) -> list[PermissionEntity]:
        return [PermissionEntity(database=database, grantee="dbo", principal_type="USER", class_desc="DATABASE", permission_name="CONNECT", state_desc="GRANT")]

    def list_database_summary(self, database: str) -> list[DatabaseSummaryEntity]:
        return [DatabaseSummaryEntity(database=database, total_tables=len(self.catalog.tables), total_views=len(self.catalog.views), total_stored_procedures=len(self.catalog.procedures), total_functions=1, total_triggers=len(self.catalog.triggers), total_users=1, total_roles=1, total_schemas=1, total_indexes=1, total_foreign_keys=len(self.catalog.foreign_keys), total_synonyms=1, total_sequences=1, total_partitions=1, total_row_count=sum(self.catalog.row_count(t.schema, t.name) for t in self.catalog.tables.values()), total_reserved_space_mb=256.0, total_used_space_mb=192.0, largest_table=max(self.catalog.tables.keys(), key=lambda key: self.catalog.size_mb(*key.split('.', 1))), largest_index="IX_Orders_CustomerId", largest_schema="dbo", last_backup="2024-06-15T00:00:00", last_restore="2024-06-16T00:00:00", recovery_model="FULL", compatibility_level="SQL Server 2022", database_size_mb=self.catalog.database_size_mb(), log_size_mb=self.catalog.log_file_size_mb(), free_space_mb=max(self.catalog.database_size_mb() - self.catalog.data_file_size_mb(), 0))]

    def list_constraints(self, database: str) -> list[ConstraintEntity]:
        c = self.catalog
        constraints: list[ConstraintEntity] = []
        for key, t in c.tables.items():
            pk_column = t.columns[0].name if t.columns else None
            if pk_column:
                constraints.append(ConstraintEntity(
                    database=database, schema=t.schema, table=t.name,
                    name=f"PK_{t.name}", constraint_type="PRIMARY_KEY",
                    columns=[pk_column], is_trusted=True, is_disabled=False, is_system_named=False,
                ))
        for from_key, to_key in c.foreign_keys:
            from_schema, from_table = from_key.split(".", 1)
            to_schema, to_table = to_key.split(".", 1)
            ref_column = f"{to_table[:-1] if to_table.endswith('s') else to_table}Id"
            constraints.append(ConstraintEntity(
                database=database, schema=from_schema, table=from_table,
                name=f"FK_{from_table}_{to_table}", constraint_type="FOREIGN_KEY",
                columns=[ref_column], referenced_table=f"{to_schema}.{to_table}",
                referenced_columns=[ref_column],
                delete_action="NO_ACTION", update_action="NO_ACTION",
                is_trusted=True, is_disabled=False, is_system_named=False,
            ))
        constraints.append(ConstraintEntity(
            database=database, schema="dbo", table="Orders", name="CK_Orders_TotalDue",
            constraint_type="CHECK", columns=["TotalDue"],
            definition="([TotalDue]>=(0))", is_trusted=True, is_disabled=False, is_system_named=False,
        ))
        constraints.append(ConstraintEntity(
            database=database, schema="dbo", table="Orders", name="DF_Orders_ModifiedDate",
            constraint_type="DEFAULT", columns=["ModifiedDate"],
            definition="(sysutcdatetime())", is_system_named=False,
        ))
        return constraints

    def list_expression_dependencies(self, database: str) -> list[tuple[str, str, str, str, str]]:
        """No live catalog exists in fixture mode -- the curated fixture
        DDL/procs already parse cleanly with sqlglot (no Command-node
        degradation), so there is nothing real to backfill and inventing
        rows here would violate the "never guess" principle."""
        return []


def extract_database_metadata(source: MetadataSource, database: str):
    """Runs the full database-level extraction for one database, isolating
    each entity type's failures via @log_object_result so e.g. a broken
    trigger query doesn't take down table extraction."""

    @log_object_result("database")
    def _databases(name):
        return source.list_databases(), "direct_metadata"

    @log_object_result("table")
    def _tables(name):
        return source.list_tables(database), "direct_metadata"

    @log_object_result("stored_procedure")
    def _procedures(name):
        return source.list_procedures(database), "direct_metadata"

    @log_object_result("trigger")
    def _triggers(name):
        return source.list_triggers(database), "direct_metadata"

    @log_object_result("agent_job")
    def _agent_jobs(name):
        return source.list_agent_jobs(), "direct_metadata"

    @log_object_result("view")
    def _views(name):
        return source.list_views(database), "direct_metadata"

    @log_object_result("foreign_key")
    def _foreign_keys(name):
        return source.list_foreign_keys(database), "direct_metadata"

    @log_object_result("database_file")
    def _database_files(name):
        return source.list_database_files(database), "direct_metadata"

    @log_object_result("index")
    def _indexes(name):
        return source.list_indexes(database), "direct_metadata"

    @log_object_result("function")
    def _functions(name):
        return source.list_functions(database), "direct_metadata"

    @log_object_result("synonym")
    def _synonyms(name):
        return source.list_synonyms(database), "direct_metadata"

    @log_object_result("sequence")
    def _sequences(name):
        return source.list_sequences(database), "direct_metadata"

    @log_object_result("user_defined_type")
    def _user_defined_types(name):
        return source.list_user_defined_types(database), "direct_metadata"

    @log_object_result("xml_schema_collection")
    def _xml_schema_collections(name):
        return source.list_xml_schema_collections(database), "direct_metadata"

    @log_object_result("assembly")
    def _assemblies(name):
        return source.list_assemblies(database), "direct_metadata"

    @log_object_result("security_principal")
    def _security_principals(name):
        return source.list_security_principals(database), "direct_metadata"

    @log_object_result("permission")
    def _permissions(name):
        return source.list_permissions(database), "direct_metadata"

    @log_object_result("database_summary")
    def _database_summary(name):
        return source.list_database_summary(database), "direct_metadata"

    @log_object_result("constraint")
    def _constraints(name):
        return source.list_constraints(database), "direct_metadata"

    log_entries = []
    databases, e = _databases(database); log_entries.append(e)
    tables, e = _tables(database); log_entries.append(e)
    procedures, e = _procedures(database); log_entries.append(e)
    triggers, e = _triggers(database); log_entries.append(e)
    agent_jobs, e = _agent_jobs(database); log_entries.append(e)
    views, e = _views(database); log_entries.append(e)
    foreign_keys, e = _foreign_keys(database); log_entries.append(e)
    database_files, e = _database_files(database); log_entries.append(e)
    indexes, e = _indexes(database); log_entries.append(e)
    functions, e = _functions(database); log_entries.append(e)
    synonyms, e = _synonyms(database); log_entries.append(e)
    sequences, e = _sequences(database); log_entries.append(e)
    user_defined_types, e = _user_defined_types(database); log_entries.append(e)
    xml_schema_collections, e = _xml_schema_collections(database); log_entries.append(e)
    assemblies, e = _assemblies(database); log_entries.append(e)
    security_principals, e = _security_principals(database); log_entries.append(e)
    permissions, e = _permissions(database); log_entries.append(e)
    database_summary, e = _database_summary(database); log_entries.append(e)
    constraints, e = _constraints(database); log_entries.append(e)

    for db_entity in databases or []:
        if db_entity.name == database:
            db_entity.table_count = len(tables or [])
            db_entity.proc_count = len(procedures or [])
            db_entity.view_count = len(views or [])

    # Aggregate counts the summary entity needs are already sitting in the
    # lists fetched above -- filled in here rather than issuing extra
    # COUNT(*) queries per entity type.
    for summary in database_summary or []:
        if summary.database == database:
            summary.total_tables = len(tables or [])
            summary.total_views = len(views or [])
            summary.total_stored_procedures = len(procedures or [])
            summary.total_functions = len(functions or [])
            summary.total_triggers = len(triggers or [])
            summary.total_indexes = len(indexes or [])
            summary.total_foreign_keys = len(foreign_keys or [])
            summary.total_synonyms = len(synonyms or [])
            summary.total_sequences = len(sequences or [])
            summary.total_users = sum(1 for p in security_principals or [] if p.principal_type == "USER")
            summary.total_roles = sum(1 for p in security_principals or [] if p.principal_type == "ROLE")
            summary.total_row_count = sum(t.row_count for t in tables or [])
            if tables:
                largest = max(tables, key=lambda t: t.size_mb)
                summary.largest_table = f"{largest.schema}.{largest.name}"
            summary.total_constraints = len(constraints or [])
            summary.total_primary_key_constraints = sum(1 for c in constraints or [] if c.constraint_type == "PRIMARY_KEY")
            summary.total_unique_constraints = sum(1 for c in constraints or [] if c.constraint_type == "UNIQUE")
            summary.total_check_constraints = sum(1 for c in constraints or [] if c.constraint_type == "CHECK")
            summary.total_default_constraints = sum(1 for c in constraints or [] if c.constraint_type == "DEFAULT")

    return {
        "databases": databases or [],
        "tables": tables or [],
        "stored_procedures": procedures or [],
        "triggers": triggers or [],
        "agent_jobs": agent_jobs or [],
        "views": views or [],
        "foreign_keys": foreign_keys or [],
        "database_files": database_files or [],
        "indexes": indexes or [],
        "functions": functions or [],
        "synonyms": synonyms or [],
        "sequences": sequences or [],
        "user_defined_types": user_defined_types or [],
        "xml_schema_collections": xml_schema_collections or [],
        "assemblies": assemblies or [],
        "security_principals": security_principals or [],
        "permissions": permissions or [],
        "database_summary": database_summary or [],
        "constraints": constraints or [],
    }, log_entries
