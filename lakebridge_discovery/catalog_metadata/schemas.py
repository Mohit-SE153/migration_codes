"""
Schema object inventory discovery, from SQL Server catalog metadata only
(sys.schemas) -- no SQL parsing, no dependence on the Analyzer report.
Confirmed absent from the Analyzer: schemas are never inventoried as their
own object by it -- they only ever appear embedded as a name-prefix on every
other object (e.g. "Sales.Store"), and its inventory has no SCHEMA-related
script category at all.

Pure object-inventory discovery -- appends to result.schemas, never
result.dependencies.

Excludes SQL Server's built-in fixed schemas (sys, INFORMATION_SCHEMA,
guest) by name -- these are engine internals with no user data, never
migration targets. "dbo" is deliberately NOT name-excluded: unlike the
other three, it routinely holds real user objects (confirmed in this exact
database: e.g. dbo.AWBuildVersion, dbo.ErrorLog), so it is a legitimate
schema to inventory like any other.

Also requires the schema to own at least one row in sys.objects. This was
added after a real-database check found it necessary, not preemptively:
SQL Server auto-creates a matching schema for every one of its 9 fixed
database roles (db_owner, db_datareader, db_ddladmin, ...) even though none
of them ever own an object -- a first pass that only excluded
sys/INFORMATION_SCHEMA/guest by name still let all 9 of those role-schemas
through. Requiring at least one owned object is a more robust exclusion
than hard-coding the "db_%" naming pattern (which would miss a custom-named
role's auto-created schema too), and is also the right semantic filter for
a migration-inventory tool: an empty schema has nothing to migrate.
"""
from __future__ import annotations

from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeObjectRef

NAME = "schemas"

_BUILT_IN_SCHEMAS = ("sys", "INFORMATION_SCHEMA", "guest")

_QUERY_SCHEMAS = f"""
SELECT s.name AS schema_name
FROM sys.schemas s
WHERE s.name NOT IN ({", ".join(f"'{s}'" for s in _BUILT_IN_SCHEMAS)})
  AND EXISTS (SELECT 1 FROM sys.objects o WHERE o.schema_id = s.schema_id)
ORDER BY s.name
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_SCHEMAS)

    seen_names: set[str] = set()
    for (schema_name,) in cursor.fetchall():
        if schema_name in seen_names:
            continue
        seen_names.add(schema_name)
        result.schemas.append(LakebridgeObjectRef(
            object_type="schema",
            name=schema_name,
            source_tech="MS SQL Server",
            raw_category="sys.schemas",
        ))
