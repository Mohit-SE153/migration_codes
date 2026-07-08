"""
Sequence object inventory discovery, from SQL Server catalog metadata only
(sys.sequences / sys.schemas) -- no SQL parsing, no dependence on the
Analyzer report. Confirmed absent from the Analyzer for the same reason as
indexes.py/constraints.py: no SEQUENCE-related script category exists in
its inventory, and the exported table DDL never includes sequence
definitions.

Pure object-inventory discovery -- appends to result.sequences, never
result.dependencies. A column DEFAULT using "NEXT VALUE FOR" a sequence is
a real Table -> Sequence dependency, discoverable the same way
computed_column_functions.py discovers Table -> Function defaults via
sys.sql_expression_dependencies -- but that's out of scope here per this
task's "inventory only, do not implement dependency relationships" goal.
This probe reports which sequence objects exist, nothing more.
"""
from __future__ import annotations

from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeObjectRef

NAME = "sequences"

_QUERY_SEQUENCES = """
SELECT s.name AS schema_name, sq.name AS sequence_name
FROM sys.sequences sq
JOIN sys.schemas s ON s.schema_id = sq.schema_id
ORDER BY s.name, sq.name
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_SEQUENCES)

    seen_names: set[str] = set()
    for schema_name, sequence_name in cursor.fetchall():
        name = f"{schema_name}.{sequence_name}"
        if name in seen_names:
            continue
        seen_names.add(name)
        result.sequences.append(LakebridgeObjectRef(
            object_type="sequence",
            name=name,
            source_tech="MS SQL Server",
            raw_category="sys.sequences",
        ))
