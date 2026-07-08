"""
Database file inventory discovery, from SQL Server catalog metadata only
(sys.database_files) -- no SQL parsing, no dependence on the Analyzer
report (it never sees this: physical file layout isn't expressible in any
exported SQL/DDL text at all, so there is no report category for it, and
none could exist).

Pure object-inventory discovery -- appends to result.database_files, never
result.dependencies. Mirrors autovista's equivalent FileEntity/
database_files category (retyped independently here -- see
DatabaseFileEntity in schema.py -- not shared code) for feature parity
between the two Discovery engines' inventory coverage.
"""
from __future__ import annotations

from lakebridge_discovery.schema import DatabaseFileEntity, LakebridgeDiscoveryResult

NAME = "database_files"

_QUERY_DATABASE_FILES = """
SELECT
    f.name AS logical_name, f.physical_name, f.type_desc,
    CAST(f.size AS BIGINT) * 8 / 1024.0 AS current_size_mb,
    CASE WHEN f.max_size = -1 THEN NULL ELSE CAST(f.max_size AS BIGINT) * 8 / 1024.0 END AS max_size_mb,
    f.is_percent_growth,
    CAST(f.growth AS BIGINT) AS growth_raw,
    ISNULL(fg.name, '') AS filegroup_name
FROM sys.database_files f
LEFT JOIN sys.filegroups fg ON fg.data_space_id = f.data_space_id
ORDER BY f.type_desc, f.name
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_DATABASE_FILES)

    seen_names: set[str] = set()
    for logical_name, physical_name, type_desc, current_size_mb, max_size_mb, is_percent_growth, growth_raw, filegroup_name in cursor.fetchall():
        if logical_name in seen_names:
            continue
        seen_names.add(logical_name)

        # growth is stored in 8KB pages when is_percent_growth=0 (-> MB via
        # /128.0), or as a raw percentage number when is_percent_growth=1 --
        # same single-field convention autovista's equivalent FileEntity
        # uses (growth_type is the discriminator, not a separate percent field).
        growth_value = float(growth_raw) if is_percent_growth else round(float(growth_raw) / 128.0, 2)
        growth_type = "PERCENT" if is_percent_growth else "MB"

        result.database_files.append(DatabaseFileEntity(
            logical_name=logical_name,
            physical_name=physical_name,
            file_type=type_desc,
            filegroup=filegroup_name or None,
            current_size_mb=round(float(current_size_mb), 2),
            max_size_mb=round(float(max_size_mb), 2) if max_size_mb is not None else None,
            growth_mb=growth_value,
            growth_type=growth_type,
        ))
