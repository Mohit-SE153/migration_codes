"""
Output contract for the Lakebridge Discovery engine. Deliberately its own
set of dataclasses -- independent of autovista/schema.py -- so the two
engines never share a schema module.

IMPORTANT: Lakebridge's Analyzer report (Excel/JSON produced by
`databricks labs lakebridge analyze`) does not have a publicly documented
field-level schema. report_parser.py maps it into the shapes below on a
best-effort basis and always sets `mapping_verified=False` until someone
runs this against a real Databricks workspace and confirms the field
mapping is correct -- see README.md "Lakebridge Discovery" section.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

Status = Literal["success", "partial", "failed", "skipped"]


@dataclass
class LakebridgeObjectRef:
    """One inventoried object from the Analyzer report, normalized as best
    we can from an undocumented report shape. `raw_category` preserves
    whatever sheet/key name it came from, for traceability."""

    object_type: str  # normalized: table | view | stored_procedure | function | trigger | synonym | schema | package | other
    name: str
    source_tech: str
    raw_category: str | None = None
    complexity: str | None = None
    notes: str | None = None
    # --- additive: SQL-Server-feature compatibility scanner (see
    # lakebridge_discovery/compatibility_scanner.py -- an independent
    # reimplementation of autovista/compatibility_scanner.py's detection,
    # never an import of it) -- named migration-risk constructs found by
    # scanning this object's exported definition text at
    # <source_export_dir>/sql/{kind}__{schema}.{name}.sql. Populated by
    # orchestrator.py after extract_dependencies() runs, for every object
    # in tables/views/stored_procedures/functions/triggers that has a
    # matching exported file; stays empty for objects with none (e.g. an
    # inventory row that came only from the Analyzer report, not this
    # run's own export).
    compatibility_flags: list[str] = field(default_factory=list)
    # --- additive: LLM-assisted remediation note (see
    # lakebridge_discovery/compatibility_remediation.py) -- short
    # plain-English explanation of the flags above, for reviewer triage
    # only. None unless compatibility_flags is non-empty and the LLM note
    # feature is enabled; never a substitute for compatibility_flags
    # itself. Independent reimplementation of autovista's own
    # compatibility_notes field -- see autovista/schema.py's ViewEntity.
    compatibility_notes: str | None = None


@dataclass
class ServerInstanceEntity:
    """Instance/server-level facts -- SERVERPROPERTY(...) and
    sys.dm_os_sys_info/sys.configurations, all server-scoped (not
    per-database). Exactly one of these per Discovery run. Field shape
    deliberately mirrors autovista.schema.ServerInstanceEntity (retyped
    independently, not imported -- see source_exporter.py's own
    SERVERPROPERTY/sys.dm_os_sys_info/sys.configurations queries) so the
    two engines' server_instance.json outputs are directly comparable."""

    product_version: str | None = None
    product_level: str | None = None
    edition: str | None = None
    engine_edition: int | None = None
    machine_name: str | None = None
    instance_name: str | None = None
    cpu_count: int | None = None
    physical_memory_mb: float | None = None
    max_server_memory_mb: int | None = None


@dataclass
class TableFeatureEntity:
    """Structural flags for one table -- temporal/memory-optimized/CDC/
    change-tracking/partitioning/compression. Populated in
    source_exporter.py's live path from sys.tables/sys.change_tracking_tables/
    sys.partitions (retyped independently of autovista/sql_metadata_extractor.py's
    equivalent queries). Lakebridge's own object inventory
    (LakebridgeObjectRef, from report_parser.py) has no room for these
    structural flags, so they're reported here as a standalone, joinable-by-
    (schema, name) list instead of being merged into it."""

    schema: str
    name: str
    is_temporal_table: bool = False
    is_memory_optimized: bool = False
    is_cdc_enabled: bool = False
    is_change_tracking_enabled: bool = False
    is_partitioned: bool = False
    partition_count: int = 0
    compression: str | None = None


@dataclass
class ProcedureParameterEntity:
    """One sys.parameters row for a stored procedure or function.
    `name` is the containing proc/function's name (schema-qualified name
    lives in `schema` + `name`), not the parameter's own name -- that's
    `parameter_name`. Standalone (not merged into LakebridgeObjectRef,
    which has no parameters field) for the same reason as
    TableFeatureEntity above."""

    schema: str
    name: str
    parameter_name: str
    data_type: str
    mode: str = "IN"


@dataclass
class ServerPrincipalEntity:
    """sys.server_principals (SQL/Windows logins, Windows groups, server
    roles), server-scoped. Retyped independently of
    autovista.schema.SecurityPrincipalEntity's server-scope rows -- no
    shared dataclass between the two engines."""

    name: str
    principal_type: str  # "LOGIN" | "SERVER_ROLE"
    is_disabled: bool | None = None
    is_fixed_role: bool | None = None
    # sys.server_role_members -- server role names this principal belongs to.
    member_of_roles: list[str] = field(default_factory=list)


@dataclass
class ServerPermissionEntity:
    """sys.server_permissions, server-scoped."""

    grantee: str
    principal_type: str
    class_desc: str | None = None
    object_name: str | None = None
    permission_name: str | None = None
    state_desc: str | None = None


@dataclass
class LinkedServerEntity:
    """sys.servers, filtered to is_linked = 1. provider_string_redacted is
    defensively scrubbed of any password=/pwd= substring the same way
    autovista.schema.LinkedServerEntity documents (retyped independently
    here -- see source_exporter.py's own _redact_connection_string)."""

    name: str
    product: str | None = None
    provider: str | None = None
    data_source: str | None = None
    provider_string_redacted: str | None = None


@dataclass
class DatabaseEntity:
    """Single-row database-level summary -- retyped independently of
    autovista.schema.DatabaseEntity (a deliberately narrower field set; see
    catalog_metadata/databases.py's module docstring for why), not shared
    code. Exactly one of these per Discovery run."""

    name: str
    size_mb: float
    table_count: int
    proc_count: int
    view_count: int
    recovery_model: str | None = None
    compatibility_level: str | None = None
    collation_name: str | None = None


@dataclass
class DatabaseFileEntity:
    """sys.database_files -- retyped independently of
    autovista.schema.FileEntity (not shared code), for feature parity
    between the two engines' inventory coverage. See
    catalog_metadata/database_files.py."""

    logical_name: str
    physical_name: str
    file_type: str | None = None  # sys.database_files.type_desc, e.g. "ROWS" | "LOG"
    filegroup: str | None = None
    current_size_mb: float = 0.0
    max_size_mb: float | None = None
    growth_mb: float | None = None  # holds a percentage number instead when growth_type="PERCENT"
    growth_type: str | None = None  # "MB" | "PERCENT"


@dataclass
class LakebridgeDependencyRef:
    source_object: str
    target_object: str
    relationship_type: str = "unknown"
    raw_category: str | None = None
    # Fields below match autovista.schema.DependencyEntity's naming so the
    # two engines' dependencies.json are structurally comparable -- see
    # dependency_extractor.py, the only producer of source_type/target_type
    # for edges found by scanning this engine's own exported SQL text.
    source_type: str = "unknown"
    target_type: str = "unknown"
    discovery_method: str = "lakebridge"
    resolved: bool = True


@dataclass
class ExportSummaryEntity:
    """What the independent source_exporter staged for the Analyzer to
    scan -- this is the "same source database" input, not a discovery
    result, but recorded here for traceability/comparison."""

    sql_definition_files: int = 0
    table_ddl_files: int = 0
    ssis_package_files: int = 0
    export_errors: list[str] = field(default_factory=list)
    export_dir: str | None = None


@dataclass
class AnalyzeInvocationEntity:
    source_tech: str
    command: list[str]
    status: Status
    exit_code: int | None = None
    duration_seconds: float | None = None
    report_json_path: str | None = None
    report_excel_path: str | None = None
    error: str | None = None
    stderr_tail: str | None = None


@dataclass
class LakebridgeLogEntry:
    stage: str  # e.g. "source_export", "analyze", "report_parse"
    object_type: str
    object_name: str
    status: Literal["success", "failed", "skipped_unchanged"]
    error: str | None = None
    duration_ms: float | None = None


@dataclass
class LakebridgeDiscoveryResult:
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float | None = None
    status: Status = "failed"
    run_mode: str = "fixture"

    export_summary: ExportSummaryEntity = field(default_factory=ExportSummaryEntity)
    analyze_invocations: list[AnalyzeInvocationEntity] = field(default_factory=list)

    # --- additive: supplementary catalog facts gathered directly by
    # source_exporter.py's own live pyodbc connection (see that module's
    # docstring) -- NOT sourced from the Analyzer report, since none of
    # this is in scope for what the Analyzer itself inventories. Server-
    # scoped (not per-database) except table_features/procedure_parameters,
    # which are scoped to the one source database this run points at.
    server_instance: ServerInstanceEntity | None = None
    table_features: list[TableFeatureEntity] = field(default_factory=list)
    procedure_parameters: list[ProcedureParameterEntity] = field(default_factory=list)
    server_principals: list[ServerPrincipalEntity] = field(default_factory=list)
    server_permissions: list[ServerPermissionEntity] = field(default_factory=list)
    linked_servers: list[LinkedServerEntity] = field(default_factory=list)

    # Best-effort normalized inventory, categorized to line up with
    # autovista's manifest categories for comparison purposes.
    tables: list[LakebridgeObjectRef] = field(default_factory=list)
    views: list[LakebridgeObjectRef] = field(default_factory=list)
    stored_procedures: list[LakebridgeObjectRef] = field(default_factory=list)
    functions: list[LakebridgeObjectRef] = field(default_factory=list)
    triggers: list[LakebridgeObjectRef] = field(default_factory=list)
    synonyms: list[LakebridgeObjectRef] = field(default_factory=list)
    schemas: list[LakebridgeObjectRef] = field(default_factory=list)
    packages: list[LakebridgeObjectRef] = field(default_factory=list)
    # Populated only by catalog_metadata's indexes.py/constraints.py/sequences.py
    # probes -- the Analyzer report has no visibility into these at all (no
    # matching inventory category, and the table DDL this engine exports to it
    # is column-only, see source_exporter.py's _reconstruct_table_ddl).
    indexes: list[LakebridgeObjectRef] = field(default_factory=list)
    constraints: list[LakebridgeObjectRef] = field(default_factory=list)
    sequences: list[LakebridgeObjectRef] = field(default_factory=list)
    # Populated only by catalog_metadata's databases.py/database_files.py
    # probes -- same "Analyzer has no visibility into this" reasoning as
    # indexes/constraints/sequences above.
    databases: list[DatabaseEntity] = field(default_factory=list)
    database_files: list[DatabaseFileEntity] = field(default_factory=list)
    # Distinct TYPE/COLLECTION *objects* (e.g. "dbo.Flag", one row per
    # user-defined type that exists), populated by catalog_metadata's
    # user_defined_types.py/xml_schema_collections.py probes -- separate
    # from and NOT a duplicate of the much larger uses_type *dependency
    # edge* counts those same probes already contribute to
    # result.dependencies (edges = how many columns/parameters USE a type;
    # this = how many distinct type objects exist). Matches autovista's own
    # user_defined_types/xml_schema_collections inventory lists, which are
    # likewise separate from its dependency graph.
    user_defined_types: list[LakebridgeObjectRef] = field(default_factory=list)
    xml_schema_collections: list[LakebridgeObjectRef] = field(default_factory=list)
    unsupported_objects: list[LakebridgeObjectRef] = field(default_factory=list)
    dependencies: list[LakebridgeDependencyRef] = field(default_factory=list)
    dependency_stats: dict = field(default_factory=dict)

    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    raw_report_paths: list[str] = field(default_factory=list)
    mapping_verified: bool = False
    mapping_notes: str = (
        "Lakebridge Analyzer's report schema is not publicly documented at the "
        "field level. This result is mapped defensively (tolerates missing/"
        "renamed fields) and has not been verified against a real Databricks "
        "workspace run. Re-validate this mapping against your first real "
        "report and update report_parser.py's key lookups if names differ."
    )

    def to_dict(self) -> dict:
        return asdict(self)
