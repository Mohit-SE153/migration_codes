"""
Per-object migration-complexity scoring.

Two independent, additive scoring models feed the same tier/hours scale
(see schema.py's ObjectComplexity and config.py's ComplexityThresholds/
EffortRubric):

  - Code/lineage objects (stored procedures, views, functions, triggers,
    SSIS embedded SQL): scored from LOC (where the manifest carries it),
    reference breadth (referenced_tables/procs/functions/sequences),
    SQL-Server-feature compatibility_flags already computed by Discovery's
    compatibility_scanner.py, dynamic SQL usage, parse health
    (parse_status/unresolved_reason), and dependency fan-in/fan-out.

  - Tables: scored from DDL feature richness (column/index/FK/trigger
    counts, temporal/CDC/change-tracking/memory-optimized/partitioned
    flags, computed/sparse/LOB column counts) -- these signals have
    nothing to do with SQL lineage, so a table gets a different formula,
    not a repurposed code-object one.

One data-shape wrinkle handled before scoring: SQL Server represents a
single multi-event trigger (e.g. "CREATE TRIGGER x ON t FOR INSERT,
UPDATE, DELETE") as one row per event in sys.trigger_events, and
Discovery's extractor carries that straight through as multiple
TriggerEntity rows sharing the same (schema, name) -- confirmed against
this build's own AdventureWorks2022 output (13 trigger rows for what
turned out to be 11 distinct trigger definitions; this is also the root
cause of the sqlglot-vs-Lakebridge "13 vs 10 triggers" count mismatch
noted in output_comparison/comparison_report.md, since Lakebridge counts
sys.triggers directly, one row per definition). Scoring each row
independently would count one trigger's migration effort 2-3x, so
_merge_duplicate_triggers() collapses them to one row per (schema, name)
before scoring -- see that function's docstring.

Every score is a documented, tunable heuristic (see each _score_* function's
weights) -- not a measured/validated estimate. scoring_reasons on each
result exists specifically so a reviewer can see *why* an object landed in
a tier without re-deriving the formula.

Reads Discovery's manifest as plain dicts (see schema.py's module
docstring for why) -- never re-parses SQL or queries a database itself.
"""
from __future__ import annotations

from collections import defaultdict

from assessment.config import AssessmentConfig
from assessment.dependency_index import DependencyIndex, build_dependency_index, object_key
from assessment.schema import ObjectComplexity

_CODE_REF_FIELDS = ("referenced_tables", "referenced_procs", "referenced_functions", "referenced_sequences")


def _merge_duplicate_triggers(triggers: list[dict]) -> list[dict]:
    """Collapses multiple TriggerEntity rows that share a (schema, name)
    into one merged row before scoring -- see module docstring for why
    these duplicates exist. List-valued reference/flag fields are unioned;
    parse health is escalated to the worst status seen in the group
    (unresolved > partially-parsed > clean); the distinct events are kept
    as `_merged_events` purely so the scoring reason can mention them."""
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for t in triggers:
        grouped[(t.get("schema"), t["name"])].append(t)

    merged: list[dict] = []
    for (_, _name), rows in grouped.items():
        if len(rows) == 1:
            merged.append(rows[0])
            continue
        base = dict(rows[0])
        for field_name in (*_CODE_REF_FIELDS, "compatibility_flags"):
            values: set = set()
            for r in rows:
                values.update(r.get(field_name) or [])
            base[field_name] = sorted(values)
        if any(r.get("parse_status") == "unresolved" for r in rows):
            base["parse_status"] = "unresolved"
        reasons = [r.get("unresolved_reason") for r in rows if r.get("unresolved_reason")]
        base["unresolved_reason"] = "; ".join(dict.fromkeys(reasons)) or None
        base["_merged_events"] = sorted({r.get("event") for r in rows if r.get("event")})
        merged.append(base)
    return merged


def _finalize(
    object_type: str, name: str, database: str, score: float, reasons: list[str],
    config: AssessmentConfig, *, loc: int = 0, compatibility_flags: list[str] | None = None,
    dynamic_sql_usage: bool = False, parse_status: str | None = None,
    unresolved_reason: str | None = None, fan_in: int = 0, fan_out: int = 0,
) -> ObjectComplexity:
    tier = config.thresholds.tier_for_score(score)
    return ObjectComplexity(
        object_type=object_type, name=name, database=database, loc=loc,
        compatibility_flags=compatibility_flags or [], dynamic_sql_usage=dynamic_sql_usage,
        parse_status=parse_status, unresolved_reason=unresolved_reason,
        fan_in=fan_in, fan_out=fan_out, complexity_score=round(score, 2), complexity_tier=tier,
        estimated_hours=config.effort_rubric.hours_for_tier(tier), scoring_reasons=reasons,
    )


def _score_code_object(
    obj: dict, object_type: str, dep_index: DependencyIndex, config: AssessmentConfig,
) -> ObjectComplexity:
    key = object_key(obj.get("schema"), obj["name"])
    reasons: list[str] = []
    score = 0.0

    loc = obj.get("loc", 0) or 0
    if loc:
        score += loc / 25.0
        reasons.append(f"{loc} LOC")

    ref_count = sum(len(obj.get(f, []) or []) for f in _CODE_REF_FIELDS)
    if ref_count:
        score += ref_count * 0.5
        reasons.append(f"{ref_count} outgoing object reference(s)")

    flags = obj.get("compatibility_flags") or []
    if flags:
        score += len(flags) * 2
        reasons.append(f"compatibility flag(s): {', '.join(sorted(flags))}")

    dynamic_sql = bool(obj.get("dynamic_sql_usage"))
    if dynamic_sql:
        score += 4
        reasons.append("dynamic SQL usage (cannot statically resolve target tables)")

    parse_status = obj.get("parse_status")
    unresolved_reason = obj.get("unresolved_reason")
    if parse_status == "unresolved":
        score += 8
        reasons.append("sqlglot could not parse this object at all")
    elif unresolved_reason:
        score += 3
        reasons.append("partially parsed -- some references may be missing")

    fan_in = dep_index.fan_in(key)
    fan_out = dep_index.fan_out(key)
    if fan_in or fan_out:
        score += 0.2 * (fan_in + fan_out)
        reasons.append(f"dependency graph fan-in={fan_in}, fan-out={fan_out}")

    merged_events = obj.get("_merged_events")
    if merged_events and len(merged_events) > 1:
        reasons.append(f"merged {len(merged_events)} per-event rows ({', '.join(merged_events)}) into one trigger")

    return _finalize(
        object_type, key, obj.get("database", ""), score, reasons, config,
        loc=loc, compatibility_flags=flags, dynamic_sql_usage=dynamic_sql,
        parse_status=parse_status, unresolved_reason=unresolved_reason,
        fan_in=fan_in, fan_out=fan_out,
    )


def _score_embedded_sql(obj: dict, dep_index: DependencyIndex, config: AssessmentConfig, database: str) -> ObjectComplexity:
    reasons: list[str] = []
    score = 0.0

    sql_text = obj.get("sql_text") or ""
    loc = len(sql_text.splitlines()) if sql_text else 0
    if loc:
        score += loc / 25.0
        reasons.append(f"{loc} LOC (embedded SQL)")

    ref_count = len(obj.get("referenced_tables", []) or []) + len(obj.get("referenced_procs", []) or []) \
        + len(obj.get("referenced_sequences", []) or [])
    if ref_count:
        score += ref_count * 0.5
        reasons.append(f"{ref_count} outgoing object reference(s)")

    flags = obj.get("compatibility_flags") or []
    if flags:
        score += len(flags) * 2
        reasons.append(f"compatibility flag(s): {', '.join(sorted(flags))}")

    parse_status = obj.get("parse_status")
    unresolved_reason = obj.get("unresolved_reason")
    if parse_status in ("unresolved", "llm_inferred"):
        score += 8
        reasons.append(f"parse_status={parse_status}")
    elif unresolved_reason:
        score += 3
        reasons.append("partially parsed -- some references may be missing")

    name = obj.get("task_name", "(unnamed task)")
    return _finalize(
        "embedded_sql", name, database, score, reasons, config,
        loc=loc, compatibility_flags=flags, parse_status=parse_status, unresolved_reason=unresolved_reason,
    )


def _score_table(table: dict, dep_index: DependencyIndex, config: AssessmentConfig) -> ObjectComplexity:
    key = object_key(table.get("schema"), table["name"])
    reasons: list[str] = []
    score = 0.0

    column_count = table.get("column_count", 0) or 0
    index_count = table.get("index_count", 0) or 0
    fk_count = table.get("foreign_key_count", 0) or 0
    trigger_count = table.get("trigger_count", 0) or 0

    score += column_count * 0.1 + index_count * 0.3 + fk_count * 0.3 + trigger_count * 1.0
    if column_count or index_count or fk_count or trigger_count:
        reasons.append(
            f"{column_count} column(s), {index_count} index(es), {fk_count} FK(s), {trigger_count} trigger(s)"
        )

    feature_flags = (
        ("is_temporal_table", 5, "system-versioned temporal table (needs Delta Lake time-travel/SCD re-design)"),
        ("is_memory_optimized", 5, "memory-optimized table (no Databricks equivalent, needs re-architecture)"),
        ("is_cdc_enabled", 6, "CDC-enabled (needs a CDC ingestion pipeline / Delta Change Data Feed re-design)"),
        ("is_change_tracking_enabled", 4, "change-tracking enabled (needs an equivalent incremental-load design)"),
        ("is_partitioned", 3, "partitioned table (needs a Delta Lake partitioning/liquid-clustering strategy)"),
    )
    for field_name, weight, reason in feature_flags:
        if table.get(field_name):
            score += weight
            reasons.append(reason)

    for list_field, weight, label in (
        ("computed_columns", 0.5, "computed column(s)"),
        ("sparse_columns", 0.3, "sparse column(s)"),
        ("lob_columns", 0.3, "LOB column(s)"),
    ):
        count = len(table.get(list_field, []) or [])
        if count:
            score += count * weight
            reasons.append(f"{count} {label}")

    fan_in = dep_index.fan_in(key)
    fan_out = dep_index.fan_out(key)
    if fan_in or fan_out:
        score += 0.2 * (fan_in + fan_out)
        reasons.append(f"dependency graph fan-in={fan_in}, fan-out={fan_out}")

    return _finalize(
        "table", key, table.get("database", ""), score, reasons, config,
        fan_in=fan_in, fan_out=fan_out,
    )


def build_object_complexity(manifest: dict, config: AssessmentConfig) -> list[ObjectComplexity]:
    """Scores every stored procedure, view, function, trigger, table, and
    SSIS embedded-SQL task in the manifest. Order of the returned list
    matches manifest iteration order (tables, then the four code-object
    categories, then packages) -- callers that want a sorted view (e.g. by
    tier or score) should sort explicitly."""
    dep_index = build_dependency_index(manifest.get("dependencies", []))
    results: list[ObjectComplexity] = []

    for table in manifest.get("tables", []):
        results.append(_score_table(table, dep_index, config))

    for object_type, field_name in (
        ("stored_procedure", "stored_procedures"),
        ("view", "views"),
        ("function", "functions"),
    ):
        for obj in manifest.get(field_name, []):
            results.append(_score_code_object(obj, object_type, dep_index, config))

    for obj in _merge_duplicate_triggers(manifest.get("triggers", [])):
        results.append(_score_code_object(obj, "trigger", dep_index, config))

    for package in manifest.get("packages", []):
        database = package.get("database", "")
        for task in package.get("tasks", []):
            for embedded in task.get("embedded_sql", []) or []:
                results.append(_score_embedded_sql(embedded, dep_index, config, database))
        for embedded in package.get("embedded_sql", []) or []:
            results.append(_score_embedded_sql(embedded, dep_index, config, database))

    return results
