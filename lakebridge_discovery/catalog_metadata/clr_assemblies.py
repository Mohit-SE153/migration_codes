"""
CLR assembly inventory discovery, from SQL Server catalog metadata only
(sys.assemblies / sys.database_principals) -- no SQL parsing, no dependence
on the Analyzer report. Confirmed absent from the Analyzer for the same
reason as indexes.py/constraints.py/sequences.py: no ASSEMBLY-related
script category exists in its inventory.

Pure object-inventory discovery -- appends to result.assemblies, reusing
LakebridgeObjectRef (same shape already used for indexes/constraints/
sequences/synonyms/schemas -- a CLR assembly is exactly the same kind of
"named object exists" fact, no dependency edges emitted).

sys.assemblies has no schema_id -- CLR assemblies are database-scoped, not
schema-scoped (same catalog fact autovista.sql_metadata_extractor.QUERY_ASSEMBLIES's
own comment documents). The closest real, catalog-backed equivalent is the
owning principal's default schema, used here the same way.

Deliberately NOT filtered to is_user_defined = 1: autovista's own
QUERY_ASSEMBLIES has no such filter either (confirmed against a real
AdventureWorks2022 instance, which has exactly one row here --
Microsoft.SqlServer.Types, a system-installed assembly for spatial types --
and autovista's assemblies.json reports it too). Filtering it out here
would silently break cross-engine count parity for this exact category.
"""
from __future__ import annotations

from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeObjectRef

NAME = "clr_assemblies"

_QUERY_ASSEMBLIES = """
SELECT dp.default_schema_name AS schema_name, a.name AS assembly_name, a.permission_set_desc, a.is_visible
FROM sys.assemblies a
LEFT JOIN sys.database_principals dp ON dp.principal_id = a.principal_id
ORDER BY a.name
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_ASSEMBLIES)

    seen_names: set[str] = set()
    for schema_name, assembly_name, permission_set_desc, is_visible in cursor.fetchall():
        name = f"{schema_name}.{assembly_name}" if schema_name else assembly_name
        if name in seen_names:
            continue
        seen_names.add(name)
        result.assemblies.append(LakebridgeObjectRef(
            object_type="clr_assembly",
            name=name,
            source_tech="MS SQL Server",
            raw_category="sys.assemblies",
            notes=f"permission_set={permission_set_desc};is_visible={bool(is_visible)}",
        ))
