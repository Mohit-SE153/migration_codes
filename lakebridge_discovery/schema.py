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


@dataclass
class LakebridgeDependencyRef:
    source_object: str
    target_object: str
    relationship_type: str = "unknown"
    raw_category: str | None = None


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
    unsupported_objects: list[LakebridgeObjectRef] = field(default_factory=list)
    dependencies: list[LakebridgeDependencyRef] = field(default_factory=list)

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
