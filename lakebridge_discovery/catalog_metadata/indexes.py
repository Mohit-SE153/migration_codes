"""
Index inventory discovery, from SQL Server catalog metadata only
(sys.indexes / sys.tables / sys.schemas) -- no SQL parsing, no dependence on
the Analyzer report. Confirmed absent from the Analyzer: its own inventory
categorization has no INDEX-related script category at all (checked against
a real report's `scriptCategories` values), and the table DDL this engine
exports to it is explicitly column-only -- see source_exporter.py's
_reconstruct_table_ddl docstring ("no defaults/constraints/indexes").

Pure object-inventory discovery -- appends to result.indexes, never
result.dependencies. An index's relationship to its own table isn't a
cross-object dependency the way a foreign key is, so this probe emits no
dependency edges at all (per this task's "inventory only" scope).

Index names are unique per-table in SQL Server, not per-schema (two
different tables can each have an index named "IX_Something"), so the
qualified name here is "schema.table.index_name" -- one more segment than
the "schema.name" convention the other catalog_metadata probes use.

Heaps (sys.indexes rows with type=0, name IS NULL) are excluded -- a heap
isn't a named, migratable index object.
"""
from __future__ import annotations

from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeObjectRef

NAME = "indexes"

_QUERY_INDEXES = """
SELECT
    s.name AS schema_name, t.name AS table_name, i.name AS index_name, i.type_desc AS index_type_desc
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE i.name IS NOT NULL
ORDER BY s.name, t.name, i.name
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_INDEXES)

    seen_names: set[str] = set()
    for schema_name, table_name, index_name, index_type_desc in cursor.fetchall():
        name = f"{schema_name}.{table_name}.{index_name}"
        if name in seen_names:
            continue
        seen_names.add(name)
        result.indexes.append(LakebridgeObjectRef(
            object_type="index",
            name=name,
            source_tech="MS SQL Server",
            raw_category="sys.indexes",
            notes=f"type={index_type_desc}",
        ))
