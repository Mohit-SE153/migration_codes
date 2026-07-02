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
    AssemblyEntity,
    ColumnEntity,
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
       COLUMNPROPERTY(c.object_id, c.name, 'SeedValue') AS identity_seed,
       COLUMNPROPERTY(c.object_id, c.name, 'IncrementValue') AS identity_increment,
       COLUMNPROPERTY(c.object_id, c.name, 'IsComputed') AS is_computed,
       COLUMNPROPERTY(c.object_id, c.name, 'IsPersisted') AS is_persisted,
       c.collation_name,
       COLUMNPROPERTY(c.object_id, c.name, 'IsRowGUIDCol') AS is_rowguid,
       c.is_sparse
FROM sys.columns c
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id
LEFT JOIN sys.check_constraints cc ON cc.object_id = c.default_object_id
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
       te.type_desc AS event
FROM sys.triggers tr
JOIN sys.tables t ON t.object_id = tr.parent_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
CROSS APPLY sys.trigger_events te WHERE te.object_id = tr.object_id
"""

QUERY_AGENT_JOBS = """
SELECT j.name, j.enabled, s.command, j.owner_sid, j.date_created, j.date_modified,
       s.retry_attempts, s.retry_interval, j.description
FROM msdb.dbo.sysjobs j
JOIN msdb.dbo.sysjobsteps s ON s.job_id = j.job_id
ORDER BY j.name, s.step_id
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
       i.fill_factor, i.data_space_id, i.filter_definition, i.object_id, i.index_id
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE i.name IS NOT NULL AND i.type_desc IN ('CLUSTERED','NONCLUSTERED')
ORDER BY s.name, t.name, i.name
"""

# Key vs. included columns, cheap catalog-view lookup (no DMV scan).
# Fragmentation %, page count, usage stats, and missing-index recommendations
# would require sys.dm_db_index_physical_stats / sys.dm_db_index_usage_stats,
# which are DMV scans that can be expensive on large tables -- intentionally
# left unpopulated (field stays NULL) rather than adding a full-table scan
# per index, per the "avoid expensive operations on large databases" guidance.
QUERY_INDEX_COLUMNS = """
SELECT c.name, ic.is_included_column
FROM sys.index_columns ic
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE ic.object_id = ? AND ic.index_id = ?
ORDER BY ic.is_included_column, ic.key_ordinal
"""

QUERY_FUNCTIONS = """
SELECT s.name AS schema_name, f.name AS function_name, f.type_desc, ty.name AS return_type, f.object_id
FROM sys.objects f
JOIN sys.schemas s ON s.schema_id = f.schema_id
LEFT JOIN sys.parameters p ON p.object_id = f.object_id AND p.parameter_id = 0
LEFT JOIN sys.types ty ON ty.user_type_id = p.user_type_id
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


class MetadataSource(Protocol):
    def list_databases(self) -> list[DatabaseEntity]: ...
    def list_tables(self, database: str) -> list[TableEntity]: ...
    def list_procedures(self, database: str) -> list[tuple[StoredProcedureEntity, str]]: ...  # (entity, definition text)
    def list_triggers(self, database: str) -> list[TriggerEntity]: ...
    def list_agent_jobs(self) -> list[AgentJobEntity]: ...
    def list_views(self, database: str) -> list[ViewEntity]: ...
    def list_foreign_keys(self, database: str) -> list[tuple[str, str]]: ...  # (from "schema.table", to "schema.table")
    def list_database_files(self, database: str) -> list[FileEntity]: ...
    def list_indexes(self, database: str) -> list[IndexEntity]: ...
    def list_functions(self, database: str) -> list[FunctionEntity]: ...
    def list_synonyms(self, database: str) -> list[SynonymEntity]: ...
    def list_sequences(self, database: str) -> list[SequenceEntity]: ...
    def list_user_defined_types(self, database: str) -> list[UserDefinedTypeEntity]: ...
    def list_xml_schema_collections(self, database: str) -> list[XmlSchemaCollectionEntity]: ...
    def list_assemblies(self, database: str) -> list[AssemblyEntity]: ...
    def list_security_principals(self, database: str) -> list[SecurityPrincipalEntity]: ...
    def list_permissions(self, database: str) -> list[PermissionEntity]: ...
    def list_database_summary(self, database: str) -> list[DatabaseSummaryEntity]: ...


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
                        identity_seed=int(c[6]) if c[6] is not None else None,
                        identity_increment=int(c[7]) if c[7] is not None else None,
                        computed_expression=None if c[8] is None else str(c[8]),
                        is_persisted=bool(c[9]) if c[9] is not None else None,
                        collation_name=c[10],
                        is_rowguid=bool(c[11]) if c[11] is not None else None,
                        is_sparse=bool(c[12]) if c[12] is not None else None,
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

    def list_triggers(self, database: str) -> list[TriggerEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_TRIGGERS)
        return [
            TriggerEntity(database=database, schema=s, name=n, table=t, event=e)
            for s, n, t, e in cur.fetchall()
        ]

    def list_agent_jobs(self) -> list[AgentJobEntity]:
        cur = self.connection.cursor()
        cur.execute(QUERY_AGENT_JOBS)
        jobs: dict[str, AgentJobEntity] = {}
        for name, enabled, command, owner_sid, date_created, date_modified, retry_attempts, retry_interval, description in cur.fetchall():
            job = jobs.setdefault(name, AgentJobEntity(name=name, enabled=bool(enabled), owner=str(owner_sid), schedule=description))
            job.steps.append(command)
            job.last_run=self._format_datetime(date_created)
            job.next_run=self._format_datetime(date_modified)
            job.retry_attempts=int(retry_attempts or 0)
            job.retry_interval=int(retry_interval or 0)
        return list(jobs.values())

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
        cur.execute(QUERY_INDEXES)
        rows = cur.fetchall()
        out = []
        for schema_name, table_name, index_name, type_desc, is_unique, has_filter, is_disabled, fill_factor, _, _, object_id, index_id in rows:
            col_cur = self.connection.cursor()
            col_cur.execute(QUERY_INDEX_COLUMNS, object_id, index_id)
            key_columns = []
            included_columns = []
            for col_name, is_included in col_cur.fetchall():
                (included_columns if is_included else key_columns).append(col_name)
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
                    key_columns=key_columns,
                    included_columns=included_columns,
                )
            )
        return out

    def list_functions(self, database: str) -> list[FunctionEntity]:
        self._use_database(database)
        cur = self.connection.cursor()
        cur.execute(QUERY_FUNCTIONS)
        return [
            FunctionEntity(database=database, schema=schema_name, name=function_name, function_type=function_type or 'SCALAR', return_type=return_type)
            for schema_name, function_name, function_type, return_type, _ in cur.fetchall()
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

    def list_triggers(self, database: str) -> list[TriggerEntity]:
        c = self.catalog
        return [
            TriggerEntity(database=database, schema=t.schema, name=t.name, table=t.table, event=t.event)
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
            IndexEntity(database=database, schema="dbo", table="Orders", name="IX_Orders_CustomerId", is_clustered=False, is_nonclustered=True, is_unique=False, is_filtered=False, is_disabled=False, fill_factor=90, compression="NONE")
        ]

    def list_functions(self, database: str) -> list[FunctionEntity]:
        return [
            FunctionEntity(database=database, schema="dbo", name="ufn_GetOrderStatus", function_type="SCALAR")
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
    }, log_entries
