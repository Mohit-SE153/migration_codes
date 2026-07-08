"""
Table -> Function dependency discovery for computed-column and
default-constraint expressions, from SQL Server catalog metadata only
(sys.computed_columns / sys.default_constraints / sys.sql_expression_dependencies
/ sys.objects / sys.schemas) -- no SQL parsing, no dependence on the
Analyzer report or dependency_extractor.py's regex gap-fill.

sys.sql_expression_dependencies is SQL Server's own resolved dependency
tracker: for a computed column, referencing_id is the *table's* object_id
and referencing_minor_id is the computed column's column_id (joined here to
sys.computed_columns to confirm the (object_id, column_id) pair really is a
computed-column definition, not e.g. a CHECK constraint referencing the same
plain column); for a default constraint, referencing_id is the constraint's
*own* object_id (default constraints are first-class sys.objects rows,
unlike computed columns), joined back to its owning table via
sys.default_constraints.parent_object_id.

"Only emit dependencies that resolve to user-defined SQL functions, ignore
built-ins" is enforced structurally by the INNER JOIN to sys.objects
filtered to type_desc IN ('SQL_SCALAR_FUNCTION', 'SQL_TABLE_VALUED_FUNCTION',
'SQL_INLINE_TABLE_VALUED_FUNCTION') -- not a blacklist, but the actual
mechanism: a built-in function call (GETDATE(), CONVERT(), ...) has no
sys.objects row at all, so it can never appear as a referenced_id in
sys.sql_expression_dependencies in the first place. The same join also
naturally excludes same-table column references a computed-column
expression can generate in this view (they resolve to the table itself,
type='U', rejected by the function-type filter).

Known limitation (see this package's design notes): sys.sql_expression_dependencies
only records what SQL Server can resolve at bind/compile time -- it cannot
see references inside dynamic SQL, and a reference it couldn't fully
resolve is marked is_ambiguous=1 rather than omitted. This probe does not
special-case is_ambiguous rows (an ambiguous row still has to join to a real
sys.objects function row to appear here at all, so it isn't a source of
false positives) -- noted as a known, inherent boundary of this catalog
view, consistent with how dynamic SQL is treated as out of reach everywhere
else in this codebase.

DISTINCT collapses multiple expressions on the same table referencing the
same function to one edge at the SQL level; seen_edges is still checked
per-row as a second, cheap layer of defense, same convention as the other
probes in this package.
"""
from __future__ import annotations

from lakebridge_discovery.catalog_metadata import vocabulary
from lakebridge_discovery.catalog_metadata.naming import name_by_key
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult

NAME = "computed_column_functions"

_FUNCTION_TYPE_DESC_FILTER = "('SQL_SCALAR_FUNCTION', 'SQL_TABLE_VALUED_FUNCTION', 'SQL_INLINE_TABLE_VALUED_FUNCTION')"

_QUERY_COMPUTED_COLUMN_FUNCTION = f"""
SELECT DISTINCT
    ts.name AS table_schema, t.name AS table_name,
    fs.name AS function_schema, f.name AS function_name
FROM sys.computed_columns cc
JOIN sys.tables t ON t.object_id = cc.object_id
JOIN sys.schemas ts ON ts.schema_id = t.schema_id
JOIN sys.sql_expression_dependencies d
    ON d.referencing_id = cc.object_id AND d.referencing_minor_id = cc.column_id
JOIN sys.objects f ON f.object_id = d.referenced_id
JOIN sys.schemas fs ON fs.schema_id = f.schema_id
WHERE f.type_desc IN {_FUNCTION_TYPE_DESC_FILTER}
ORDER BY ts.name, t.name, fs.name, f.name
"""

_QUERY_DEFAULT_CONSTRAINT_FUNCTION = f"""
SELECT DISTINCT
    ts.name AS table_schema, t.name AS table_name,
    fs.name AS function_schema, f.name AS function_name
FROM sys.default_constraints dc
JOIN sys.tables t ON t.object_id = dc.parent_object_id
JOIN sys.schemas ts ON ts.schema_id = t.schema_id
JOIN sys.sql_expression_dependencies d ON d.referencing_id = dc.object_id
JOIN sys.objects f ON f.object_id = d.referenced_id
JOIN sys.schemas fs ON fs.schema_id = f.schema_id
WHERE f.type_desc IN {_FUNCTION_TYPE_DESC_FILTER}
ORDER BY ts.name, t.name, fs.name, f.name
"""


def _emit(
    result: LakebridgeDiscoveryResult, seen_edges: set[tuple],
    table_schema: str, table_name: str, function_schema: str, function_name: str, table_names: dict[str, str],
) -> None:
    key = f"{table_schema.lower()}.{table_name.lower()}"
    source_object = table_names.get(key, f"{table_schema}.{table_name}")
    target_object = f"{function_schema.lower()}.{function_name.lower()}"

    edge_key = (source_object, target_object, vocabulary.CALLS)
    if edge_key in seen_edges:
        return
    seen_edges.add(edge_key)

    result.dependencies.append(LakebridgeDependencyRef(
        source_object=source_object,
        target_object=target_object,
        relationship_type=vocabulary.CALLS,
        raw_category=vocabulary.RAW_CATEGORY_COMPUTED_COLUMN_FUNCTION,
        source_type=vocabulary.TABLE,
        target_type=vocabulary.FUNCTION,
        discovery_method=vocabulary.DISCOVERY_METHOD,
        resolved=True,
    ))


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    table_names = name_by_key(result, "tables")

    cursor = connection.cursor()
    cursor.execute(_QUERY_COMPUTED_COLUMN_FUNCTION)
    for table_schema, table_name, function_schema, function_name in cursor.fetchall():
        _emit(result, seen_edges, table_schema, table_name, function_schema, function_name, table_names)

    cursor = connection.cursor()
    cursor.execute(_QUERY_DEFAULT_CONSTRAINT_FUNCTION)
    for table_schema, table_name, function_schema, function_name in cursor.fetchall():
        _emit(result, seen_edges, table_schema, table_name, function_schema, function_name, table_names)
