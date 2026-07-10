"""
LLM-based per-object complexity judgment -- the third method for scoring
migration complexity in this repo, alongside assessment/complexity_scorer.py
(a fixed point-formula heuristic) and lakebridge_assessment/complexity_mapper.py
(Lakebridge Analyzer's own native rating).

Deliberately metadata-only (per this build's own scope decision): the LLM
is given the same structured signals the heuristic scorer already
computes (LOC, reference breadth, compatibility_flags, dynamic SQL usage,
parse health, dependency fan-in/fan-out) -- never raw SQL source text, so
this works against the sqlglot Discovery manifest as-is, with no
dependency on a separate source-export step. The point of comparison is
"does an LLM judge the same facts differently than our formula," not
"can an LLM read code the other two methods can't."

Hard rules, same non-negotiable contract as autovista/llm_fallback_extractor.py
and autovista/compatibility_remediation.py:
  - Never a source of truth -- every tier is LLM judgment over metadata,
    explicitly labeled as such in scoring_reasons and in the manifest-level
    warning this phase's orchestrator adds.
  - Deterministic guardrails: strict output schema, and a hard cap
    (config.max_objects_per_run) on how many objects get sent per run.
  - If no API key is configured, or the cap is reached, remaining objects
    are excluded from object_complexity entirely (not guessed a tier) --
    see the returned stats dict's `skipped_no_client`/`skipped_capped`
    counts for exactly how many and why.
  - One object's LLM failure (network error, malformed JSON) never fails
    the run -- that object is simply excluded, counted under `failed`.

Fully self-contained: _merge_duplicate_triggers below is this package's
own copy (not imported from assessment/complexity_scorer.py), so this
module keeps working even if assessment/ is deleted later -- see
schema.py's module docstring for the same rationale applied repo-wide in
this package.
"""
from __future__ import annotations

from collections import defaultdict

from llm_assessment.config import LlmAssessmentConfig
from llm_assessment.dependency_index import build_dependency_index, object_key
from llm_assessment.llm_client import LlmClient
from llm_assessment.logging_setup import logger
from llm_assessment.schema import ObjectComplexity

_CODE_REF_FIELDS = ("referenced_tables", "referenced_procs", "referenced_functions", "referenced_sequences")


def _merge_duplicate_triggers(triggers: list[dict]) -> list[dict]:
    """SQL Server represents one multi-event trigger (e.g. FOR INSERT,
    UPDATE, DELETE) as one row per event in sys.trigger_events, and
    Discovery's extractor carries that straight through as multiple
    TriggerEntity rows sharing the same (schema, name). Scoring each row
    independently would call the LLM (and count effort) 2-3x for one real
    trigger, so same-named rows are collapsed here first -- own copy of
    assessment/complexity_scorer.py's identical fix, not imported."""
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


SYSTEM_PROMPT = """You are assisting a SQL Server -> Databricks/Delta Lake migration assessment.

You will be given structured metadata about ONE database object extracted by a discovery \
pipeline -- NOT the object's source code, only its type, size/complexity signals, any \
SQL-Server-feature compatibility flags already detected (e.g. MERGE, PIVOT, LINKED_SERVER, \
XP_CMDSHELL), its position in the dependency graph (fan-in/fan-out), and its parse health. \
Using only this metadata, judge how complex this object will be to migrate to Databricks.

Tier guidance:
- Low: trivial/mechanical port, no special constructs.
- Medium: some rework needed but well-understood (e.g. a couple of compatibility flags with a known Databricks equivalent).
- High: significant rework, or a construct with limited/indirect Databricks tooling support.
- Critical: no direct Databricks equivalent exists at all, or the object requires manual re-architecture.

Respond ONLY with JSON matching this schema, no prose:
{"tier": "Low"|"Medium"|"High"|"Critical", "confidence": "low"|"medium"|"high", "rationale": "<=400 chars explaining your judgment from the metadata given"}"""

_VALID_TIERS = {"Low", "Medium", "High", "Critical"}


def _describe_code_object(obj: dict, object_type: str, fan_in: int, fan_out: int) -> str:
    ref_counts = ", ".join(f"{len(obj.get(f, []) or [])} {f.replace('referenced_', '')}" for f in _CODE_REF_FIELDS)
    parse_status = obj.get("parse_status") or "unknown"
    unresolved_reason = obj.get("unresolved_reason")
    flags = obj.get("compatibility_flags") or []
    lines = [
        f"Object: {object_type} {object_key(obj.get('schema'), obj['name'])}",
        f"Lines of code: {obj.get('loc', 'unknown')}",
        f"Outgoing references: {ref_counts}",
        f"Dependency graph: fan-in={fan_in}, fan-out={fan_out}",
        f"Dynamic SQL usage: {'yes' if obj.get('dynamic_sql_usage') else 'no'}",
        f"Parse status: {parse_status}" + (f" ({unresolved_reason})" if unresolved_reason else ""),
        f"Compatibility flags: {', '.join(sorted(flags)) if flags else 'none'}",
    ]
    return "\n".join(lines)


def _describe_table(table: dict, fan_in: int, fan_out: int) -> str:
    features = ", ".join(
        f"{label}={'yes' if table.get(field_name) else 'no'}"
        for field_name, label in (
            ("is_temporal_table", "temporal"), ("is_memory_optimized", "memory_optimized"),
            ("is_cdc_enabled", "cdc"), ("is_change_tracking_enabled", "change_tracking"),
            ("is_partitioned", "partitioned"),
        )
    )
    lines = [
        f"Object: table {object_key(table.get('schema'), table['name'])}",
        f"Columns: {table.get('column_count', 0)}, Indexes: {table.get('index_count', 0)}, "
        f"Foreign keys: {table.get('foreign_key_count', 0)}, Triggers: {table.get('trigger_count', 0)}",
        f"Special features: {features}",
        f"Computed columns: {len(table.get('computed_columns', []) or [])}, "
        f"Sparse columns: {len(table.get('sparse_columns', []) or [])}, "
        f"LOB columns: {len(table.get('lob_columns', []) or [])}",
        f"Dependency graph: fan-in={fan_in}, fan-out={fan_out}",
    ]
    return "\n".join(lines)


def build_object_complexity(
    manifest: dict, config: LlmAssessmentConfig, client: LlmClient | None,
) -> tuple[list[ObjectComplexity], dict]:
    """Returns (scored objects, stats). stats has keys: attempted,
    succeeded, failed, skipped_no_client, skipped_capped."""
    dep_index = build_dependency_index(manifest.get("dependencies", []))
    results: list[ObjectComplexity] = []
    stats = {"attempted": 0, "succeeded": 0, "failed": 0, "skipped_no_client": 0, "skipped_capped": 0}

    candidates: list[tuple[str, dict, str]] = []  # (object_type, obj, description)
    for table in manifest.get("tables", []):
        key = object_key(table.get("schema"), table["name"])
        candidates.append(("table", table, _describe_table(table, dep_index.fan_in(key), dep_index.fan_out(key))))
    for object_type, field_name in (
        ("stored_procedure", "stored_procedures"), ("view", "views"), ("function", "functions"),
    ):
        for obj in manifest.get(field_name, []):
            key = object_key(obj.get("schema"), obj["name"])
            candidates.append((object_type, obj, _describe_code_object(obj, object_type, dep_index.fan_in(key), dep_index.fan_out(key))))
    for obj in _merge_duplicate_triggers(manifest.get("triggers", [])):
        key = object_key(obj.get("schema"), obj["name"])
        candidates.append(("trigger", obj, _describe_code_object(obj, "trigger", dep_index.fan_in(key), dep_index.fan_out(key))))

    for object_type, obj, description in candidates:
        name = object_key(obj.get("schema"), obj["name"])

        if client is None:
            stats["skipped_no_client"] += 1
            continue
        if stats["attempted"] >= config.max_objects_per_run:
            stats["skipped_capped"] += 1
            continue

        stats["attempted"] += 1
        try:
            raw = client.complete_json(SYSTEM_PROMPT, description)
            tier = raw.get("tier")
            if tier not in _VALID_TIERS:
                raise ValueError(f"model returned an unrecognized tier: {tier!r}")
            confidence = raw.get("confidence", "low")
            rationale = raw.get("rationale", "")
        except Exception as exc:  # noqa: BLE001 - one object's LLM failure must not fail the run
            stats["failed"] += 1
            logger.warning("[%d/%d] FAIL %-16s %-40s error=%s", stats["attempted"], len(candidates), object_type, name, exc)
            continue

        logger.info("[%d/%d] OK   %-16s %-40s tier=%s", stats["attempted"], len(candidates), object_type, name, tier)
        stats["succeeded"] += 1
        results.append(ObjectComplexity(
            object_type=object_type, name=name, database=obj.get("database", ""),
            loc=obj.get("loc", 0) or 0, compatibility_flags=obj.get("compatibility_flags") or [],
            dynamic_sql_usage=bool(obj.get("dynamic_sql_usage")),
            parse_status=obj.get("parse_status"), unresolved_reason=obj.get("unresolved_reason"),
            fan_in=dep_index.fan_in(name), fan_out=dep_index.fan_out(name),
            complexity_score=float({"Low": 1, "Medium": 2, "High": 3, "Critical": 4}[tier]),
            complexity_tier=tier, estimated_hours=config.effort_rubric.hours_for_tier(tier),
            scoring_reasons=[f"LLM judgment (model={config.model}, confidence={confidence}): {rationale}"],
        ))

    return results, stats
