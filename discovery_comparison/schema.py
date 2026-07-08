"""
Output contract for the Discovery Comparison module -- independent of both
engines' schemas. This module only ever *reads* each engine's already-
written output files; it does not import autovista or lakebridge_discovery
modules, so a change to either engine's internals can't silently break it
beyond the file contract each already documents/writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

EngineStatus = Literal["success", "partial", "failed", "not_run"]


@dataclass
class EngineRunSummary:
    engine: str
    status: EngineStatus
    duration_seconds: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_count: int = 0
    warning_count: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class CategoryComparison:
    category: str
    sqlglot_count: int = 0
    lakebridge_count: int = 0
    difference: int = 0
    matched_count: int = 0
    sqlglot_only_sample: list[str] = field(default_factory=list)
    lakebridge_only_sample: list[str] = field(default_factory=list)
    match_basis: str = "best-effort normalized name match -- not guaranteed precise, see README"


@dataclass
class ComparisonResult:
    generated_at: str = ""
    sqlglot_run: EngineRunSummary = field(default_factory=lambda: EngineRunSummary(engine="sqlglot", status="not_run"))
    lakebridge_run: EngineRunSummary = field(default_factory=lambda: EngineRunSummary(engine="lakebridge", status="not_run"))
    categories: list[CategoryComparison] = field(default_factory=list)
    sqlglot_dependency_count: int = 0
    lakebridge_dependency_count: int = 0
    notes: list[str] = field(default_factory=list)

    # --- additive: dependency-type breakdown (each engine's own
    # dependency_stats.json, read verbatim, not recomputed here) -- lets the
    # report show relationship_type/discovery_method counts side by side
    # without this module re-deriving them from dependencies.json itself.
    sqlglot_dependency_stats: dict = field(default_factory=dict)
    lakebridge_dependency_stats: dict = field(default_factory=dict)

    # --- additive: category -> plain-English note for categories that are
    # generated/derived artifacts rather than native SQL Server catalog
    # objects (Database Summary, Data Quality Summary, Unsupported Objects,
    # Warnings, ...) -- see comparator.GENERATED_ARTIFACT_NOTES. Kept
    # separate from the free-text `notes` list above (which is for
    # run-level operational notes) so report_writer.py can render these as
    # their own labeled section.
    category_notes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
