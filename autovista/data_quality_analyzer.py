"""
Metadata-driven Data Quality Summary (Discovery Enhancement 4).

Computes migration-readiness indicators purely from entities other
extractors have already collected (tables, columns, indexes, constraints)
-- issues no SQL of its own and performs no full-table scans, per the
Discovery-phase's metadata-only philosophy.

Deliberately NOT included: estimated NULL percentages per column. A
reliable estimate needs either a full scan or column statistics
histograms (sys.dm_db_stats_histogram / DBCC SHOW_STATISTICS), which is
more than "metadata-only" -- left for a future, opt-in pass rather than
guessed at here.
"""
from __future__ import annotations

from autovista.schema import ConstraintEntity, DataQualitySummaryEntity, IndexEntity, TableEntity

_DEPRECATED_DATA_TYPES = {"text", "ntext", "image", "timestamp"}
_TEXT_NTEXT_IMAGE_TYPES = {"text", "ntext", "image"}
_SPATIAL_DATA_TYPES = {"geography", "geometry"}
_LOB_DATA_TYPES = {"text", "ntext", "image", "varchar", "nvarchar", "varbinary", "xml"}
_MAX_LENGTH_ELIGIBLE_TYPES = {"varchar", "nvarchar", "varbinary"}
_MAX_LENGTH_SENTINEL = -1  # sys.columns.max_length == -1 means "(MAX)"

WIDE_SCHEMA_COLUMN_THRESHOLD = 50
EXCESSIVE_INDEX_THRESHOLD = 10
LARGEST_TABLES_TOP_N = 10


def build_data_quality_summary(
    database: str,
    tables: list[TableEntity],
    indexes: list[IndexEntity],
    constraints: list[ConstraintEntity],
) -> DataQualitySummaryEntity:
    summary = DataQualitySummaryEntity(database=database)
    summary.total_tables = len(tables)
    if not tables:
        return summary

    tables_with_pk = {(c.schema, c.table) for c in constraints if c.constraint_type == "PRIMARY_KEY"}
    tables_with_fk = {(c.schema, c.table) for c in constraints if c.constraint_type == "FOREIGN_KEY"}
    clustered_tables = {(idx.schema, idx.table) for idx in indexes if idx.is_clustered}
    index_count_by_table: dict[tuple, int] = {}
    for idx in indexes:
        key = (idx.schema, idx.table)
        index_count_by_table[key] = index_count_by_table.get(key, 0) + 1

    seen_column_names: dict[str, int] = {}

    for t in tables:
        key = (t.schema, t.name)
        if t.row_count == 0:
            summary.empty_tables += 1
        if key not in tables_with_pk:
            summary.tables_without_primary_key += 1
        if key not in clustered_tables:
            summary.tables_without_clustered_index += 1
        if key not in tables_with_fk:
            summary.tables_without_foreign_key += 1
        if t.table_type == "HEAP":
            summary.heap_tables += 1
        if t.trigger_count:
            summary.tables_with_triggers += 1
        if t.identity_columns:
            summary.tables_with_identity_columns += 1
        if t.computed_columns:
            summary.tables_with_computed_columns += 1
        if t.sparse_columns:
            summary.tables_with_sparse_columns += 1
        if t.is_cdc_enabled:
            summary.tables_with_cdc_enabled += 1
        if t.is_change_tracking_enabled:
            summary.tables_with_change_tracking_enabled += 1
        if t.is_temporal_table:
            summary.tables_with_temporal_tables += 1

        has_xml = has_spatial = has_clr = has_lob = has_filestream = False
        for col in t.columns:
            data_type = (col.data_type or "").lower()
            seen_column_names[col.name] = seen_column_names.get(col.name, 0) + 1

            if col.nullable:
                summary.nullable_columns += 1
            else:
                summary.non_nullable_columns += 1

            if data_type in _DEPRECATED_DATA_TYPES:
                summary.deprecated_data_type_columns += 1
            if data_type == "sql_variant":
                summary.sql_variant_columns += 1
            if data_type in _TEXT_NTEXT_IMAGE_TYPES:
                summary.text_ntext_image_columns += 1
            if data_type in _MAX_LENGTH_ELIGIBLE_TYPES and col.max_length == _MAX_LENGTH_SENTINEL:
                summary.large_max_columns += 1

            if data_type == "xml":
                has_xml = True
            if data_type in _SPATIAL_DATA_TYPES:
                has_spatial = True
            if col.is_clr_type:
                has_clr = True
            if data_type in _LOB_DATA_TYPES:
                has_lob = True
            if col.is_filestream:
                has_filestream = True

        if has_xml:
            summary.tables_with_xml_columns += 1
        if has_spatial:
            summary.tables_with_spatial_columns += 1
        if has_clr:
            summary.tables_with_clr_types += 1
        if has_lob:
            summary.tables_with_lob_columns += 1
        if has_filestream:
            summary.tables_with_filestream += 1

    summary.duplicate_column_names = sum(1 for count in seen_column_names.values() if count > 1)

    total_row_bytes = sum(t.size_mb * 1024 * 1024 for t in tables)
    total_rows = sum(t.row_count for t in tables)
    summary.average_row_length_bytes = round(total_row_bytes / total_rows, 2) if total_rows else None

    largest = sorted(tables, key=lambda t: t.size_mb, reverse=True)[:LARGEST_TABLES_TOP_N]
    summary.largest_tables = [f"{t.schema}.{t.name}" for t in largest]

    summary.wide_schema_tables = [
        f"{t.schema}.{t.name}" for t in tables if t.column_count > WIDE_SCHEMA_COLUMN_THRESHOLD
    ]
    summary.excessive_index_tables = [
        f"{schema}.{table}" for (schema, table), count in index_count_by_table.items()
        if count > EXCESSIVE_INDEX_THRESHOLD
    ]

    return summary
