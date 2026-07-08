"""
Table -> Table foreign-key dependency discovery, from SQL Server catalog
metadata only (sys.foreign_keys / sys.tables / sys.schemas) -- no SQL
parsing, no dependence on the Analyzer report or this engine's own regex
gap-fill (dependency_extractor.py).

sys.foreign_keys already has exactly one row per FK *constraint*, not per
column, so a composite multi-column FK naturally produces exactly one
Table -> Table edge here. sys.foreign_key_columns (column-level detail) is
deliberately not queried: nothing beyond the constraint-level parent/
referenced table pair is needed for this dependency category, and querying
it would only mean grouping column-rows back down to the same one-edge-per-
constraint result sys.foreign_keys already gives directly.

Self-referencing FKs (a table's own FK referencing itself, e.g. an
employee-hierarchy table with a ManagerID -> BusinessEntityID constraint on
itself) are deliberately NOT filtered as a self-loop here -- unlike
dependency_extractor.py's/report_parser.py's code-lineage self-loop
suppression (which exists to drop false positives, like a function's own
header matching its own name), a self-referencing FK is a real, meaningful
structural fact for migration ordering and must be retained.

Object-name casing intentionally mirrors the rest of this pipeline's
convention without reusing dependency_extractor.py's text-scan helpers (see
this package's design notes): source_object keeps this run's own inventory
casing (falling back to the catalog's casing if the table isn't in
result.tables yet); target_object is always lowercased "schema.table",
matching how every other target in dependencies.json is normalized once
resolved. Both endpoints are catalog-verified to be real tables, so
resolved=True and target_type="table" unconditionally -- SQL Server's own
referential integrity guarantees the referenced table exists.
"""
from __future__ import annotations

from lakebridge_discovery.catalog_metadata import vocabulary
from lakebridge_discovery.catalog_metadata.naming import name_by_key
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult

NAME = "foreign_keys"

_QUERY_FOREIGN_KEYS = """
SELECT
    ps.name AS parent_schema, pt.name AS parent_table,
    rs.name AS referenced_schema, rt.name AS referenced_table
FROM sys.foreign_keys fk
JOIN sys.tables pt ON pt.object_id = fk.parent_object_id
JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
ORDER BY ps.name, pt.name, fk.name
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    table_names = name_by_key(result, "tables")

    cursor = connection.cursor()
    cursor.execute(_QUERY_FOREIGN_KEYS)
    rows = cursor.fetchall()

    for parent_schema, parent_table, referenced_schema, referenced_table in rows:
        parent_key = f"{parent_schema.lower()}.{parent_table.lower()}"
        source_object = table_names.get(parent_key, f"{parent_schema}.{parent_table}")
        target_object = f"{referenced_schema.lower()}.{referenced_table.lower()}"

        edge_key = (source_object, target_object, vocabulary.FOREIGN_KEY)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)

        result.dependencies.append(LakebridgeDependencyRef(
            source_object=source_object,
            target_object=target_object,
            relationship_type=vocabulary.FOREIGN_KEY,
            raw_category=vocabulary.RAW_CATEGORY_FOREIGN_KEYS,
            source_type=vocabulary.TABLE,
            target_type=vocabulary.TABLE,
            discovery_method=vocabulary.DISCOVERY_METHOD,
            resolved=True,
        ))
