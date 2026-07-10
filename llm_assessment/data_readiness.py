"""
Migration-readiness rollup built from Discovery's data_quality_summary.
Fully self-contained independent copy (not imported from
assessment/data_readiness.py) -- see schema.py's module docstring for why.
"""
from __future__ import annotations

from llm_assessment.schema import DataReadinessFinding

_COUNT_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("tables_without_primary_key", "Medium", "table(s) have no primary key defined",
     "Add an explicit business/surrogate key -- Delta Lake MERGE/upsert patterns and most BI tools expect one, even though Databricks doesn't enforce PK constraints by default."),
    ("heap_tables", "Low", "table(s) are heaps (no clustered index)",
     "Databricks has no clustered-index concept; use Liquid Clustering on frequently filtered columns for equivalent query performance."),
    ("tables_without_foreign_key", "Low", "table(s) have no recorded foreign key relationship",
     "Confirm whether this is intentional or missing metadata before dropping referential-integrity checks during migration."),
    ("empty_tables", "Low", "table(s) currently contain zero rows",
     "Confirm whether these are legitimately empty (e.g. staging tables) or dead objects that don't need migrating at all."),
    ("tables_with_cdc_enabled", "High", "table(s) have Change Data Capture (CDC) enabled",
     "SQL Server CDC has no direct Databricks equivalent; re-architect via a CDC ingestion tool (e.g. Debezium) or Delta Lake's Change Data Feed."),
    ("tables_with_change_tracking_enabled", "Medium", "table(s) have Change Tracking enabled",
     "Needs an equivalent incremental-load design (e.g. Delta Change Data Feed or a watermark-based pattern)."),
    ("tables_with_temporal_tables", "Medium", "table(s) are system-versioned temporal tables",
     "Re-implement via Delta Lake time-travel queries or an explicit SCD Type 2 pattern; SQL Server's automatic history table has no direct equivalent."),
    ("tables_with_filestream", "Critical", "table(s) use FILESTREAM columns",
     "No Databricks equivalent; migrate the referenced files to cloud object storage (e.g. a Volumes-backed path) and store the path/reference instead."),
    ("tables_with_clr_types", "Critical", "table(s) have CLR-typed columns",
     "CLR user-defined types must be rewritten as a native Spark type plus supporting UDF logic before migration."),
    ("tables_with_spatial_columns", "High", "table(s) have geography/geometry (spatial) columns",
     "Databricks lacks native spatial types; use a geospatial library (e.g. Mosaic, H3) or store as WKT/WKB in a string/binary column."),
    ("tables_with_xml_columns", "Medium", "table(s) have XML-typed columns",
     "Databricks has no native XML type; parse to STRUCT/STRING (from_xml) or store as a raw STRING column."),
    ("sql_variant_columns", "Medium", "column(s) use the sql_variant data type",
     "sql_variant has no Databricks equivalent; each column must be resolved to a single concrete type (or split into typed columns) before migration."),
    ("text_ntext_image_columns", "Medium", "column(s) use deprecated text/ntext/image types",
     "Convert to STRING or BINARY before migration -- these types have no direct Databricks equivalent."),
    ("deprecated_data_type_columns", "Low", "column(s) use other deprecated data types",
     "Review column-by-column and map to a modern equivalent type during schema migration."),
    ("duplicate_column_names", "Medium", "duplicate column name(s) detected",
     "Investigate before migration -- Delta Lake requires unique column names (case-insensitive by default)."),
    ("large_max_columns", "Low", "column(s) use MAX-length types (varchar(max)/nvarchar(max)/varbinary(max))",
     "Generally fine on Databricks (STRING/BINARY are unbounded), but review for any downstream size-sensitive processing."),
)

_SAMPLE_LIST_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("wide_schema_tables", "Medium", "table(s) flagged as unusually wide (many columns)",
     "Wide tables may benefit from column pruning or splitting during modeling; review before a straight lift-and-shift."),
    ("excessive_index_tables", "Low", "table(s) flagged with an excessive number of indexes",
     "Databricks/Delta Lake doesn't use traditional B-tree indexes; review each for a Liquid Clustering candidate instead of a 1:1 index migration."),
    ("largest_tables", "Low", "largest table(s) by size, useful for cluster/storage sizing planning",
     "No action required by default; use these to inform initial cluster sizing and ingestion batching strategy -- see infra_sizing.py for a concrete recommendation."),
)


def build_data_readiness(manifest: dict) -> list[DataReadinessFinding]:
    summaries = manifest.get("data_quality_summary", [])
    if not summaries:
        return []

    findings: list[DataReadinessFinding] = []

    for field_name, severity, label, recommendation in _COUNT_FIELDS:
        total = sum(s.get(field_name, 0) or 0 for s in summaries)
        if total:
            findings.append(DataReadinessFinding(
                category=field_name, count=total, severity=severity,
                description=f"{total} {label}", recommendation=recommendation,
            ))

    for field_name, severity, label, recommendation in _SAMPLE_LIST_FIELDS:
        samples: list[str] = []
        for s in summaries:
            samples.extend(s.get(field_name, []) or [])
        if samples:
            findings.append(DataReadinessFinding(
                category=field_name, count=len(samples), severity=severity,
                description=f"{len(samples)} {label}", recommendation=recommendation,
                sample_objects=samples,
            ))

    return findings
