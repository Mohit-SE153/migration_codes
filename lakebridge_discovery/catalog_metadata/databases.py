"""
Database-level summary inventory discovery, from SQL Server catalog
metadata only (sys.databases / DATABASEPROPERTYEX / a cheap sys.tables+
sys.dm_db_partition_stats size estimate) -- no SQL parsing, no dependence
on the Analyzer report (it has no database-level entity at all: it only
ever sees the individual exported .sql files, never queries sys.databases
itself).

Pure object-inventory discovery -- appends the single current database's
summary row to result.databases, never result.dependencies. Deliberately a
narrower field set than autovista's DatabaseEntity (which has 20+ optional
fields covering backup/restore history, containment, snapshot isolation,
etc.) -- this covers the core identity/sizing fields realistically needed
for cross-engine parity (name, size, object counts, recovery model,
compatibility level) without replicating autovista's full backup-history
querying, which is a materially larger, separate piece of work.
"""
from __future__ import annotations

from lakebridge_discovery.schema import DatabaseEntity, LakebridgeDiscoveryResult

NAME = "databases"

_QUERY_DATABASE_SUMMARY = """
SELECT
    DB_NAME() AS database_name,
    (SELECT CAST(SUM(size) AS BIGINT) * 8 / 1024.0 FROM sys.database_files) AS size_mb,
    (SELECT COUNT(*) FROM sys.tables) AS table_count,
    (SELECT COUNT(*) FROM sys.objects WHERE type = 'P') AS proc_count,
    (SELECT COUNT(*) FROM sys.views) AS view_count,
    DATABASEPROPERTYEX(DB_NAME(), 'Recovery') AS recovery_model,
    CAST(DATABASEPROPERTYEX(DB_NAME(), 'Version') AS NVARCHAR(20)) AS compatibility_version,
    DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS collation_name
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_DATABASE_SUMMARY)
    row = cursor.fetchone()
    if row is None:
        return

    (database_name, size_mb, table_count, proc_count, view_count,
     recovery_model, compatibility_version, collation_name) = row

    result.databases.append(DatabaseEntity(
        name=database_name,
        size_mb=round(float(size_mb), 2) if size_mb is not None else 0.0,
        table_count=int(table_count) if table_count is not None else 0,
        proc_count=int(proc_count) if proc_count is not None else 0,
        view_count=int(view_count) if view_count is not None else 0,
        recovery_model=recovery_model,
        compatibility_level=compatibility_version,
        collation_name=collation_name,
    ))
