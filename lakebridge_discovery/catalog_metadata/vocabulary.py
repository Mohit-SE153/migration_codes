"""
Centralized string constants for catalog_metadata's probes: object types,
relationship types, discovery method, and raw_category tags.

Every probe module imports from here instead of hand-writing literals, so as
more probes are added over time (indexes, constraints, sequences,
partition_functions, security_objects, ...) the vocabulary can't silently
drift between files the way report_parser.py's own docstring warns
_CATEGORY_KEYWORDS/_PLURAL exist to prevent for the Analyzer-report side.
"""
from __future__ import annotations

# Tags every edge this package emits, distinguishing catalog-derived edges
# from the Analyzer-native ("lakebridge_report") and regex-gap-fill
# ("lakebridge") edges in dependencies.json / dependency_stats.json.
DISCOVERY_METHOD = "catalog_metadata"

# object_type / source_type / target_type values this package's probes use.
TABLE = "table"
STORED_PROCEDURE = "stored_procedure"
FUNCTION = "function"
USER_DEFINED_TYPE = "user_defined_type"
XML_SCHEMA_COLLECTION = "xml_schema_collection"

# relationship_type values this package's probes use.
FOREIGN_KEY = "foreign_key"
USES_TYPE = "uses_type"
CALLS = "calls"

# raw_category values -- one per catalog-view combination a probe queries,
# for per-edge provenance traceability (mirrors report_parser.py/
# dependency_extractor.py's existing "text_scan"/"object_lineage" tagging).
RAW_CATEGORY_FOREIGN_KEYS = "sys.foreign_keys"
RAW_CATEGORY_TABLE_UDT = "sys.columns+sys.types"
RAW_CATEGORY_PROC_UDT = "sys.parameters+sys.types"
RAW_CATEGORY_XML_SCHEMA_COLLECTION = "sys.xml_schema_collections"
RAW_CATEGORY_COMPUTED_COLUMN_FUNCTION = "sys.sql_expression_dependencies"
