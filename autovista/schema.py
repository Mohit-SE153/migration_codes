"""
Output contract for the Discovery phase. These dataclasses are the single
source of truth for the JSON manifest shape consumed by the downstream
Assessment phase -- see README.md "Output schema" for field-level docs.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

ParseStatus = Literal["direct_metadata", "sqlglot", "xml_parsed", "llm_inferred", "unresolved"]


@dataclass
class DiscoveryLogEntry:
    object_type: str
    object_name: str
    status: Literal["success", "failed", "skipped_unchanged"]
    parse_status: ParseStatus | None = None
    error: str | None = None
    duration_ms: float | None = None


@dataclass
class DatabaseEntity:
    name: str
    size_mb: float
    table_count: int
    proc_count: int
    view_count: int
    data_file_size_mb: float = 0.0
    log_file_size_mb: float = 0.0
    data_occupied_pct: float | None = None
    log_occupied_pct: float | None = None
    recovery_model: str | None = None
    compatibility_level: str | None = None
    database_owner: str | None = None
    collation_name: str | None = None
    create_date: str | None = None
    last_backup_date: str | None = None
    last_full_backup: str | None = None
    last_differential_backup: str | None = None
    last_log_backup: str | None = None
    last_restore_date: str | None = None
    auto_close: bool | None = None
    auto_shrink: bool | None = None
    is_read_only: bool | None = None
    is_trustworthy_on: bool | None = None
    page_verify_option: str | None = None
    containment: str | None = None
    is_snapshot_isolation_on: bool | None = None
    is_read_committed_snapshot_on: bool | None = None
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class ColumnEntity:
    name: str
    data_type: str
    nullable: bool
    ordinal_position: int
    default_constraint: str | None = None
    check_constraint: str | None = None
    identity_seed: int | None = None
    identity_increment: int | None = None
    computed_expression: str | None = None
    is_persisted: bool | None = None
    collation_name: str | None = None
    is_indexed: bool | None = None
    is_part_of_pk: bool | None = None
    is_part_of_fk: bool | None = None
    is_rowguid: bool | None = None
    is_sparse: bool | None = None
    is_nullable: bool | None = None

    def __post_init__(self) -> None:
        if self.is_nullable is None:
            self.is_nullable = self.nullable


@dataclass
class ParameterEntity:
    name: str
    data_type: str
    mode: str = "IN"


@dataclass
class TableEntity:
    database: str
    schema: str
    name: str
    row_count: int
    size_mb: float
    column_count: int
    columns: list[ColumnEntity] = field(default_factory=list)
    create_date: str | None = None
    modify_date: str | None = None
    table_type: str | None = None
    compression: str | None = None
    index_count: int = 0
    nonclustered_index_count: int = 0
    foreign_key_count: int = 0
    referenced_table_count: int = 0
    referencing_table_count: int = 0
    trigger_count: int = 0
    identity_columns: list[str] = field(default_factory=list)
    computed_columns: list[str] = field(default_factory=list)
    sparse_columns: list[str] = field(default_factory=list)
    rowguid_columns: list[str] = field(default_factory=list)
    lob_columns: list[str] = field(default_factory=list)
    is_temporal_table: bool | None = None
    is_memory_optimized: bool | None = None
    is_cdc_enabled: bool | None = None
    is_change_tracking_enabled: bool | None = None
    is_partitioned: bool | None = None
    partition_count: int = 0
    estimated_reserved_pages: int = 0
    used_pages: int = 0
    data_pages: int = 0
    percent_of_database_occupied: float | None = None
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class FileEntity:
    database: str
    logical_name: str
    physical_name: str
    filegroup: str | None = None
    current_size_mb: float = 0.0
    max_size_mb: float | None = None
    growth_mb: float | None = None
    growth_type: str | None = None
    percent_of_total_database: float | None = None


@dataclass
class ViewEntity:
    database: str
    schema: str
    name: str
    referenced_tables: list[str] = field(default_factory=list)
    create_date: str | None = None
    modify_date: str | None = None
    is_indexed_view: bool | None = None
    is_schema_bound: bool | None = None
    referenced_objects: list[str] = field(default_factory=list)
    referencing_objects: list[str] = field(default_factory=list)
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class TriggerEntity:
    database: str
    schema: str
    name: str
    table: str
    event: str
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class AgentJobEntity:
    name: str
    enabled: bool
    steps: list[str] = field(default_factory=list)
    schedule: str | None = None
    frequency: str | None = None
    last_run: str | None = None
    next_run: str | None = None
    last_outcome: str | None = None
    owner: str | None = None
    retry_attempts: int | None = None
    retry_interval: int | None = None
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class StoredProcedureEntity:
    database: str
    schema: str
    name: str
    loc: int
    referenced_tables: list[str] = field(default_factory=list)
    referenced_procs: list[str] = field(default_factory=list)
    referenced_functions: list[str] = field(default_factory=list)
    create_date: str | None = None
    modify_date: str | None = None
    is_encrypted: bool | None = None
    execute_as: str | None = None
    parameters: list[ParameterEntity] = field(default_factory=list)
    parameter_count: int = 0
    dynamic_sql_usage: bool | None = None
    parse_status: ParseStatus = "direct_metadata"
    unresolved_reason: str | None = None


@dataclass
class IndexEntity:
    database: str
    schema: str
    table: str
    name: str
    is_clustered: bool = False
    is_nonclustered: bool = False
    is_unique: bool = False
    is_filtered: bool = False
    is_disabled: bool = False
    fill_factor: int | None = None
    compression: str | None = None
    fragmentation_pct: float | None = None
    page_count: int | None = None
    index_size_mb: float | None = None
    included_columns: list[str] = field(default_factory=list)
    key_columns: list[str] = field(default_factory=list)
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class FunctionEntity:
    database: str
    schema: str
    name: str
    function_type: str
    return_type: str | None = None
    parameters: list[ParameterEntity] = field(default_factory=list)
    parameter_count: int = 0
    referenced_objects: list[str] = field(default_factory=list)
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class SynonymEntity:
    database: str
    schema: str
    name: str
    base_object: str
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class SequenceEntity:
    database: str
    schema: str
    name: str
    current_value: int | None = None
    increment: int | None = None
    minimum_value: int | None = None
    maximum_value: int | None = None
    cache: int | None = None
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class UserDefinedTypeEntity:
    database: str
    schema: str
    name: str
    type_kind: str
    base_type: str | None = None
    is_nullable: bool | None = None
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class XmlSchemaCollectionEntity:
    database: str
    schema: str
    name: str
    xml_namespace: str | None = None
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class AssemblyEntity:
    database: str
    schema: str
    name: str
    permission_set: str | None = None
    is_visible: bool | None = None
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class SecurityPrincipalEntity:
    database: str
    name: str
    principal_type: str
    default_schema: str | None = None
    owning_principal: str | None = None
    is_fixed_role: bool | None = None
    is_disabled: bool | None = None
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class PermissionEntity:
    database: str
    grantee: str
    principal_type: str
    class_desc: str | None = None
    object_name: str | None = None
    permission_name: str | None = None
    state_desc: str | None = None
    parse_status: ParseStatus = "direct_metadata"


@dataclass
class DatabaseSummaryEntity:
    database: str
    total_tables: int = 0
    total_views: int = 0
    total_stored_procedures: int = 0
    total_functions: int = 0
    total_triggers: int = 0
    total_users: int = 0
    total_roles: int = 0
    total_schemas: int = 0
    total_indexes: int = 0
    total_foreign_keys: int = 0
    total_synonyms: int = 0
    total_sequences: int = 0
    total_partitions: int = 0
    total_row_count: int = 0
    total_reserved_space_mb: float = 0.0
    total_used_space_mb: float = 0.0
    largest_table: str | None = None
    largest_index: str | None = None
    largest_schema: str | None = None
    last_backup: str | None = None
    last_restore: str | None = None
    recovery_model: str | None = None
    compatibility_level: str | None = None
    database_size_mb: float = 0.0
    log_size_mb: float = 0.0
    free_space_mb: float = 0.0


@dataclass
class EmbeddedSqlEntity:
    task_name: str
    task_type: str
    sql_text: str
    referenced_tables: list[str] = field(default_factory=list)
    referenced_procs: list[str] = field(default_factory=list)
    parse_status: ParseStatus = "xml_parsed"
    unresolved_reason: str | None = None


@dataclass
class SsisTaskEntity:
    name: str
    task_type: str
    parent_container: str | None = None
    embedded_sql: list[EmbeddedSqlEntity] = field(default_factory=list)
    executed_package: str | None = None  # set for Execute Package Task
    unparseable_body: bool = False  # e.g. Script Task source code


@dataclass
class ConnectionManagerEntity:
    name: str
    creation_name: str
    connection_string_redacted: str


@dataclass
class PackageVariableEntity:
    name: str
    namespace: str
    data_type: str


@dataclass
class PrecedenceConstraintEntity:
    from_task: str
    to_task: str
    evaluation_value: str  # e.g. Success / Failure / Completion


@dataclass
class PackageEntity:
    name: str
    project: str
    deployment_model: Literal["ssisdb", "file_system"]
    folder: str = ""  # SSISDB folder (catalog deployment_model only); "" for file_system
    tasks: list[SsisTaskEntity] = field(default_factory=list)
    connection_managers: list[ConnectionManagerEntity] = field(default_factory=list)
    variables: list[PackageVariableEntity] = field(default_factory=list)
    precedence_constraints: list[PrecedenceConstraintEntity] = field(default_factory=list)
    embedded_sql: list[EmbeddedSqlEntity] = field(default_factory=list)
    parse_status: ParseStatus = "xml_parsed"


@dataclass
class DependencyEntity:
    source_object: str
    source_type: str
    target_object: str
    target_type: str
    relationship_type: str
    discovery_method: ParseStatus


@dataclass
class DiscoveryManifest:
    databases: list[DatabaseEntity] = field(default_factory=list)
    tables: list[TableEntity] = field(default_factory=list)
    views: list[ViewEntity] = field(default_factory=list)
    triggers: list[TriggerEntity] = field(default_factory=list)
    agent_jobs: list[AgentJobEntity] = field(default_factory=list)
    stored_procedures: list[StoredProcedureEntity] = field(default_factory=list)
    packages: list[PackageEntity] = field(default_factory=list)
    dependencies: list[DependencyEntity] = field(default_factory=list)
    database_files: list[FileEntity] = field(default_factory=list)
    indexes: list[IndexEntity] = field(default_factory=list)
    functions: list[FunctionEntity] = field(default_factory=list)
    synonyms: list[SynonymEntity] = field(default_factory=list)
    sequences: list[SequenceEntity] = field(default_factory=list)
    user_defined_types: list[UserDefinedTypeEntity] = field(default_factory=list)
    xml_schema_collections: list[XmlSchemaCollectionEntity] = field(default_factory=list)
    assemblies: list[AssemblyEntity] = field(default_factory=list)
    security_principals: list[SecurityPrincipalEntity] = field(default_factory=list)
    permissions: list[PermissionEntity] = field(default_factory=list)
    database_summary: list[DatabaseSummaryEntity] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
