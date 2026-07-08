"""
Database-level summary rollup discovery. Deliberately does NOT re-query
every catalog view its counts describe: it aggregates counts already
gathered by this run's own inventory (result.tables/views/stored_procedures/
functions/triggers from the Analyzer report; result.schemas/indexes/
constraints/sequences/synonyms/database_users/database_roles from this
package's own earlier probes) -- see _REGISTRY in __init__.py, where this
probe is registered LAST specifically so every other probe/category it
reads from has already run in the same pass. This is the "do not duplicate
existing discovery" principle applied directly: no second sys.tables/
sys.indexes/etc. query for a count this run already computed once.

The only genuinely new catalog fact this probe queries directly is
largest_table (sys.tables/sys.partitions row-count ranking) -- nothing else
in this run's inventory carries per-table size/row information.

If run with a narrower LAKEBRIDGE_CATALOG_METADATA_SOURCES allowlist that
excludes one of the probes this reads from, that category's count is simply
0 (or None) here -- the same honest-degradation behavior as any other probe
in this package, not a bug to work around.

Pure object-inventory discovery -- appends the single current database's
summary row to result.database_summary, never result.dependencies.
"""
from __future__ import annotations

from lakebridge_discovery.schema import DatabaseSummaryEntity, LakebridgeDiscoveryResult

NAME = "database_summary"

_QUERY_LARGEST_TABLE = """
SELECT TOP 1 s.name + '.' + t.name AS qualified_name
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
GROUP BY s.name, t.name
ORDER BY SUM(p.rows) DESC
"""

_FOREIGN_KEY_CONSTRAINT_NOTE = "FOREIGN_KEY_CONSTRAINT"


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_LARGEST_TABLE)
    row = cursor.fetchone()
    largest_table = row[0] if row else None

    db = result.databases[0] if result.databases else None
    total_foreign_keys = sum(1 for c in result.constraints if c.notes == _FOREIGN_KEY_CONSTRAINT_NOTE)

    result.database_summary.append(DatabaseSummaryEntity(
        database=db.name if db else "",
        total_tables=len(result.tables),
        total_views=len(result.views),
        total_stored_procedures=len(result.stored_procedures),
        total_functions=len(result.functions),
        total_triggers=len(result.triggers),
        total_users=len(result.database_users),
        total_roles=len(result.database_roles),
        total_schemas=len(result.schemas),
        total_indexes=len(result.indexes),
        total_foreign_keys=total_foreign_keys,
        total_synonyms=len(result.synonyms),
        total_sequences=len(result.sequences),
        total_constraints=len(result.constraints),
        database_size_mb=db.size_mb if db else 0.0,
        recovery_model=db.recovery_model if db else None,
        compatibility_level=db.compatibility_level if db else None,
        largest_table=largest_table,
    ))
