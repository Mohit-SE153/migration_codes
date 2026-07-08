"""
Constraint inventory discovery, from SQL Server catalog metadata only
(sys.key_constraints / sys.check_constraints / sys.default_constraints /
sys.foreign_keys / sys.schemas) -- no SQL parsing, no dependence on the
Analyzer report. Confirmed absent from the Analyzer for the same reason as
indexes.py: no CONSTRAINT-related script category exists in its inventory,
and the exported table DDL never contains constraint definitions.

Pure object-inventory discovery -- appends to result.constraints, never
result.dependencies. This does NOT duplicate or replace foreign_keys.py's
Table -> Table foreign-key *dependency edges* (Stage 2 of the catalog_metadata
package): a foreign key is both a dependency edge (this table depends on
that table existing) and a named constraint object in its own right (e.g.
FK_SalesOrderHeader_SalesTerritory). This probe inventories the latter;
foreign_keys.py already covers the former; neither reads nor writes the
other's output, and this probe emits zero dependency edges.

Constraint names (PRIMARY KEY, UNIQUE, CHECK, DEFAULT, FOREIGN KEY) are
unique per schema in SQL Server, same as tables/views/procedures, so
"schema.name" is sufficient (unlike indexes.py, which needs table-scoping
since index names are only unique per-table).
"""
from __future__ import annotations

from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeObjectRef

NAME = "constraints"

_QUERY_KEY_CONSTRAINTS = """
SELECT s.name AS schema_name, kc.name AS constraint_name, kc.type_desc AS constraint_type_desc
FROM sys.key_constraints kc
JOIN sys.schemas s ON s.schema_id = kc.schema_id
ORDER BY s.name, kc.name
"""

_QUERY_CHECK_CONSTRAINTS = """
SELECT s.name AS schema_name, cc.name AS constraint_name
FROM sys.check_constraints cc
JOIN sys.schemas s ON s.schema_id = cc.schema_id
ORDER BY s.name, cc.name
"""

_QUERY_DEFAULT_CONSTRAINTS = """
SELECT s.name AS schema_name, dc.name AS constraint_name
FROM sys.default_constraints dc
JOIN sys.schemas s ON s.schema_id = dc.schema_id
ORDER BY s.name, dc.name
"""

_QUERY_FOREIGN_KEY_CONSTRAINTS = """
SELECT s.name AS schema_name, fk.name AS constraint_name
FROM sys.foreign_keys fk
JOIN sys.schemas s ON s.schema_id = fk.schema_id
ORDER BY s.name, fk.name
"""


def _emit(
    result: LakebridgeDiscoveryResult, seen_names: set[str],
    schema_name: str, constraint_name: str, constraint_type_desc: str, raw_category: str,
) -> None:
    name = f"{schema_name}.{constraint_name}"
    if name in seen_names:
        return
    seen_names.add(name)
    result.constraints.append(LakebridgeObjectRef(
        object_type="constraint",
        name=name,
        source_tech="MS SQL Server",
        raw_category=raw_category,
        notes=constraint_type_desc,
    ))


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    seen_names: set[str] = set()

    cursor = connection.cursor()
    cursor.execute(_QUERY_KEY_CONSTRAINTS)
    for schema_name, constraint_name, constraint_type_desc in cursor.fetchall():
        _emit(result, seen_names, schema_name, constraint_name, constraint_type_desc, "sys.key_constraints")

    cursor = connection.cursor()
    cursor.execute(_QUERY_CHECK_CONSTRAINTS)
    for schema_name, constraint_name in cursor.fetchall():
        _emit(result, seen_names, schema_name, constraint_name, "CHECK_CONSTRAINT", "sys.check_constraints")

    cursor = connection.cursor()
    cursor.execute(_QUERY_DEFAULT_CONSTRAINTS)
    for schema_name, constraint_name in cursor.fetchall():
        _emit(result, seen_names, schema_name, constraint_name, "DEFAULT_CONSTRAINT", "sys.default_constraints")

    cursor = connection.cursor()
    cursor.execute(_QUERY_FOREIGN_KEY_CONSTRAINTS)
    for schema_name, constraint_name in cursor.fetchall():
        _emit(result, seen_names, schema_name, constraint_name, "FOREIGN_KEY_CONSTRAINT", "sys.foreign_keys")
