"""
Synonym object inventory discovery, from SQL Server catalog metadata only
(sys.synonyms / sys.schemas) -- no SQL parsing, no dependence on the
Analyzer report. Confirmed absent from the Analyzer: no SYNONYM-related
script category exists in its inventory, and this database in particular
has zero synonym objects to report in the first place (confirmed directly
against sys.synonyms -- 0 rows), so there was nothing for either the
Analyzer or a text scan to ever find here.

Pure object-inventory discovery -- appends to result.synonyms, never
result.dependencies. A synonym's base_object_name is a real reference
relationship (Synonym -> underlying object) that could be discovered as a
dependency edge the same way foreign_keys.py discovers Table -> Table, but
that's out of scope for this task's "inventory only" goal -- this probe
reports which synonym objects exist, nothing more.
"""
from __future__ import annotations

from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeObjectRef

NAME = "synonyms"

_QUERY_SYNONYMS = """
SELECT s.name AS schema_name, syn.name AS synonym_name, syn.base_object_name
FROM sys.synonyms syn
JOIN sys.schemas s ON s.schema_id = syn.schema_id
ORDER BY s.name, syn.name
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_SYNONYMS)

    seen_names: set[str] = set()
    for schema_name, synonym_name, base_object_name in cursor.fetchall():
        name = f"{schema_name}.{synonym_name}"
        if name in seen_names:
            continue
        seen_names.add(name)
        result.synonyms.append(LakebridgeObjectRef(
            object_type="synonym",
            name=name,
            source_tech="MS SQL Server",
            raw_category="sys.synonyms",
            notes=f"base_object={base_object_name}",
        ))
