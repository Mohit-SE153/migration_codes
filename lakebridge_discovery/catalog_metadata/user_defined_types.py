"""
Table -> User Defined Type and Procedure/Function -> User Defined Type
dependency discovery, from SQL Server catalog metadata only (sys.columns /
sys.parameters / sys.types / sys.schemas) -- no SQL parsing, no dependence
on the Analyzer report or dependency_extractor.py's regex gap-fill.

Two related catalog facts, one dependency category: a table column typed
with a user-defined type (sys.columns.user_type_id -> sys.types) and a
stored procedure/function parameter typed the same way
(sys.parameters.user_type_id -> sys.types) are both instances of "this
object depends on this user-defined type existing", so both are emitted as
relationship_type="uses_type" from this one probe module -- matching the
target architecture's one-file-per-category shape (this category is
"user_defined_types", not split per source-object kind).

sys.types.is_user_defined=1 covers both T-SQL alias types (CREATE TYPE ...
FROM ...) and user-defined table types (CREATE TYPE ... AS TABLE, used for
table-valued parameters) -- both are included here rather than arbitrarily
excluding table types, since they're the same catalog fact and the same
"depends on this type" relationship.

Routines: sys.parameters naturally covers stored procedures AND functions
(scalar, table-valued, inline table-valued) -- both are included rather than
narrowing to stored procedures only, since excluding function-parameter UDT
usage would be an arbitrary, avoidable gap. source_type is set per-row from
sys.objects.type_desc (never hardcoded), so a function parameter is
correctly tagged "function" and a stored procedure parameter
"stored_procedure" -- type_desc is used instead of the raw padded char(2)
sys.objects.type code, matching source_exporter.py's existing convention for
the identical classification problem.

DISTINCT in both queries collapses multiple same-typed columns/parameters on
the same object to one edge at the SQL level (e.g. two dbo.Flag columns on
one table); seen_edges provides a second, cheap layer of defense against
duplicates across passes/probes, same convention as foreign_keys.py.
"""
from __future__ import annotations

from lakebridge_discovery.catalog_metadata import vocabulary
from lakebridge_discovery.catalog_metadata.naming import name_by_key
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult

NAME = "user_defined_types"

_QUERY_TABLE_UDT = """
SELECT DISTINCT
    ts.name AS table_schema, t.name AS table_name,
    tys.name AS type_schema, ty.name AS type_name
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.schemas ts ON ts.schema_id = t.schema_id
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
JOIN sys.schemas tys ON tys.schema_id = ty.schema_id
WHERE ty.is_user_defined = 1
ORDER BY ts.name, t.name, tys.name, ty.name
"""

_QUERY_ROUTINE_UDT = """
SELECT DISTINCT
    os.name AS object_schema, o.name AS object_name, o.type_desc AS object_type_desc,
    tys.name AS type_schema, ty.name AS type_name
FROM sys.parameters p
JOIN sys.objects o ON o.object_id = p.object_id
JOIN sys.schemas os ON os.schema_id = o.schema_id
JOIN sys.types ty ON ty.user_type_id = p.user_type_id
JOIN sys.schemas tys ON tys.schema_id = ty.schema_id
WHERE ty.is_user_defined = 1
  AND o.type_desc IN ('SQL_STORED_PROCEDURE', 'SQL_SCALAR_FUNCTION', 'SQL_TABLE_VALUED_FUNCTION', 'SQL_INLINE_TABLE_VALUED_FUNCTION')
ORDER BY os.name, o.name, tys.name, ty.name
"""

_ROUTINE_TYPE_DESC_TO_SOURCE_TYPE = {
    "SQL_STORED_PROCEDURE": vocabulary.STORED_PROCEDURE,
    "SQL_SCALAR_FUNCTION": vocabulary.FUNCTION,
    "SQL_TABLE_VALUED_FUNCTION": vocabulary.FUNCTION,
    "SQL_INLINE_TABLE_VALUED_FUNCTION": vocabulary.FUNCTION,
}


def _emit(
    result: LakebridgeDiscoveryResult, seen_edges: set[tuple], source_object: str, source_type: str,
    type_schema: str, type_name: str, raw_category: str,
) -> None:
    target_object = f"{type_schema.lower()}.{type_name.lower()}"
    edge_key = (source_object, target_object, vocabulary.USES_TYPE)
    if edge_key in seen_edges:
        return
    seen_edges.add(edge_key)
    result.dependencies.append(LakebridgeDependencyRef(
        source_object=source_object,
        target_object=target_object,
        relationship_type=vocabulary.USES_TYPE,
        raw_category=raw_category,
        source_type=source_type,
        target_type=vocabulary.USER_DEFINED_TYPE,
        discovery_method=vocabulary.DISCOVERY_METHOD,
        resolved=True,
    ))


def _discover_table_udt(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    table_names = name_by_key(result, "tables")
    cursor = connection.cursor()
    cursor.execute(_QUERY_TABLE_UDT)
    for table_schema, table_name, type_schema, type_name in cursor.fetchall():
        key = f"{table_schema.lower()}.{table_name.lower()}"
        source_object = table_names.get(key, f"{table_schema}.{table_name}")
        _emit(result, seen_edges, source_object, vocabulary.TABLE, type_schema, type_name, vocabulary.RAW_CATEGORY_TABLE_UDT)


def _discover_routine_udt(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    routine_names = name_by_key(result, "stored_procedures", "functions")
    cursor = connection.cursor()
    cursor.execute(_QUERY_ROUTINE_UDT)
    for object_schema, object_name, object_type_desc, type_schema, type_name in cursor.fetchall():
        source_type = _ROUTINE_TYPE_DESC_TO_SOURCE_TYPE.get(object_type_desc)
        if source_type is None:
            continue  # defensive -- the WHERE clause already restricts to known routine kinds
        key = f"{object_schema.lower()}.{object_name.lower()}"
        source_object = routine_names.get(key, f"{object_schema}.{object_name}")
        _emit(result, seen_edges, source_object, source_type, type_schema, type_name, vocabulary.RAW_CATEGORY_PROC_UDT)


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    _discover_table_udt(connection, result, seen_edges)
    _discover_routine_udt(connection, result, seen_edges)
