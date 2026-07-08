"""
Metadata-driven Data Quality Summary, from SQL Server catalog metadata only
(sys.tables / sys.columns / sys.types / sys.indexes / sys.key_constraints /
sys.foreign_keys / sys.triggers / sys.identity_columns / sys.computed_columns
/ sys.change_tracking_tables) -- independent reimplementation of
autovista.data_quality_analyzer.build_data_quality_summary's category set,
not shared code and not derived from autovista's TableEntity/ColumnEntity
(this engine has no equivalent rich per-table/per-column entity to reuse --
its own object inventory, LakebridgeObjectRef, comes from the Analyzer
report and carries no column/row/index detail at all).

Two direct queries only (one per-table, one per-column) -- no per-table
round-trips. excessive_index_tables reuses this run's own result.indexes
(populated earlier by indexes.py, if that probe ran) rather than a third
query, grouping its "schema.table.index_name" strings by their
"schema.table" prefix; this is the one category here that IS derived from
already-collected data rather than a fresh catalog query, for the same
"don't duplicate existing discovery" reason database_summary.py documents.

Deliberately NOT covered (kept out for the same "no full-table scan, no
column-statistics histogram" reason autovista's own module documents, plus
a handful of categories that would need extra joins this pass chose not to
add): duplicate_column_names, average_row_length_bytes, wide_schema_tables,
tables_without_clustered_index, tables_with_sparse/xml/spatial/clr/lob/
filestream columns. A future pass can add these the same way this probe
was added, without changing anything here.

Pure object-inventory discovery -- appends the single current database's
summary row to result.data_quality_summary, never result.dependencies.
"""
from __future__ import annotations

from collections import Counter

from lakebridge_discovery.schema import DataQualitySummaryEntity, LakebridgeDiscoveryResult

NAME = "data_quality_summary"

LARGEST_TABLES_TOP_N = 10
EXCESSIVE_INDEX_THRESHOLD = 10

_DEPRECATED_DATA_TYPES = {"text", "ntext", "image", "timestamp"}
_TEXT_NTEXT_IMAGE_TYPES = {"text", "ntext", "image"}
_MAX_LENGTH_ELIGIBLE_TYPES = {"varchar", "nvarchar", "varbinary"}
_MAX_LENGTH_SENTINEL = -1

_QUERY_TABLE_FACTS = """
SELECT
    s.name AS schema_name, t.name AS table_name,
    ISNULL(ps.row_count, 0) AS row_count,
    CASE WHEN EXISTS (SELECT 1 FROM sys.indexes i WHERE i.object_id = t.object_id AND i.type = 1) THEN 0 ELSE 1 END AS is_heap,
    CASE WHEN EXISTS (SELECT 1 FROM sys.key_constraints kc WHERE kc.parent_object_id = t.object_id AND kc.type = 'PK') THEN 1 ELSE 0 END AS has_pk,
    CASE WHEN EXISTS (SELECT 1 FROM sys.foreign_keys fk WHERE fk.parent_object_id = t.object_id) THEN 1 ELSE 0 END AS has_fk,
    CASE WHEN EXISTS (SELECT 1 FROM sys.triggers tr WHERE tr.parent_id = t.object_id) THEN 1 ELSE 0 END AS has_trigger,
    CASE WHEN EXISTS (SELECT 1 FROM sys.identity_columns ic WHERE ic.object_id = t.object_id) THEN 1 ELSE 0 END AS has_identity,
    CASE WHEN EXISTS (SELECT 1 FROM sys.computed_columns cc WHERE cc.object_id = t.object_id) THEN 1 ELSE 0 END AS has_computed,
    CASE WHEN t.is_tracked_by_cdc = 1 THEN 1 ELSE 0 END AS has_cdc,
    CASE WHEN EXISTS (SELECT 1 FROM sys.change_tracking_tables ctt WHERE ctt.object_id = t.object_id) THEN 1 ELSE 0 END AS has_change_tracking,
    CASE WHEN t.temporal_type <> 0 THEN 1 ELSE 0 END AS is_temporal
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
OUTER APPLY (SELECT SUM(p.rows) AS row_count FROM sys.partitions p WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)) ps
ORDER BY s.name, t.name
"""

_QUERY_COLUMN_FACTS = """
SELECT ty.name AS type_name, c.is_nullable, c.max_length
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
"""


def _excessive_index_tables(result: LakebridgeDiscoveryResult) -> list[str]:
    counts: Counter = Counter()
    for idx in result.indexes:
        schema_table, _, _index_name = idx.name.rpartition(".")
        if schema_table:
            counts[schema_table] += 1
    return sorted(name for name, count in counts.items() if count > EXCESSIVE_INDEX_THRESHOLD)


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_TABLE_FACTS)
    table_rows = cursor.fetchall()

    summary = DataQualitySummaryEntity(database=result.databases[0].name if result.databases else "")
    summary.total_tables = len(table_rows)

    ranked_by_rows: list[tuple[int, str]] = []
    for schema_name, table_name, row_count, is_heap, has_pk, has_fk, has_trigger, has_identity, has_computed, has_cdc, has_change_tracking, is_temporal in table_rows:
        qualified_name = f"{schema_name}.{table_name}"
        ranked_by_rows.append((int(row_count or 0), qualified_name))

        if not row_count:
            summary.empty_tables += 1
        if not has_pk:
            summary.tables_without_primary_key += 1
        if not has_fk:
            summary.tables_without_foreign_key += 1
        if is_heap:
            summary.heap_tables += 1
        if has_trigger:
            summary.tables_with_triggers += 1
        if has_identity:
            summary.tables_with_identity_columns += 1
        if has_computed:
            summary.tables_with_computed_columns += 1
        if has_cdc:
            summary.tables_with_cdc_enabled += 1
        if has_change_tracking:
            summary.tables_with_change_tracking_enabled += 1
        if is_temporal:
            summary.tables_with_temporal_tables += 1

    ranked_by_rows.sort(reverse=True)
    summary.largest_tables = [name for _, name in ranked_by_rows[:LARGEST_TABLES_TOP_N]]

    cursor = connection.cursor()
    cursor.execute(_QUERY_COLUMN_FACTS)
    for type_name, is_nullable, max_length in cursor.fetchall():
        data_type = (type_name or "").lower()
        if is_nullable:
            summary.nullable_columns += 1
        else:
            summary.non_nullable_columns += 1
        if data_type in _DEPRECATED_DATA_TYPES:
            summary.deprecated_data_type_columns += 1
        if data_type == "sql_variant":
            summary.sql_variant_columns += 1
        if data_type in _TEXT_NTEXT_IMAGE_TYPES:
            summary.text_ntext_image_columns += 1
        if data_type in _MAX_LENGTH_ELIGIBLE_TYPES and max_length == _MAX_LENGTH_SENTINEL:
            summary.large_max_columns += 1

    summary.excessive_index_tables = _excessive_index_tables(result)

    result.data_quality_summary.append(summary)
