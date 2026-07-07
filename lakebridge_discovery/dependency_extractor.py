"""
Gap-filling dependency extraction, purely from Lakebridge Discovery's own
data -- never from SQLGlot. This is deliberately the *fallback*, not the
primary source: report_parser.py's extract_native_report_dependencies()
already pulls structured dependency edges straight out of the Analyzer's own
JSON report (Bladespector's `objectRel`/`subJobInfo`/`object_lineage`
fields), which is a real analyzer's own understanding of each object's
references, not a guess. This module regex-scans the verbatim SQL text this
run staged for the Analyzer (source_exporter.py, at
<source_export_dir>/sql/{kind}__{schema}.{name}.sql) for *every* source
object, but only ever adds an edge that isn't already known -- see the
`seen_edges` pre-seeding in extract_dependencies() below. It used to skip an
object's text entirely once the report gave it any native edge at all, but a
real-report comparison against SQLGlot's independently-computed dependency
graph (see the code-review history around 2026-07-06) showed the Analyzer's
`object_lineage` shape structurally never produces "calls" edges (no
execute/lookup action exists in that shape, only reads/writes-to-a-target),
so object-level skipping was silently dropping real EXEC-based
stored-procedure calls for every trigger/proc the report *did* cover for
table reads/writes. Edge-level dedup fixes that while still being strictly
additive over the native pass.

This is a plain-regex scan, not a SQL parser -- intentionally, so this
engine never shares parsing logic with autovista/sql_lineage_parser.py.
Every reference it finds is resolved against the object inventory
report_parser.py already collected (result.tables/views/stored_procedures/
functions/triggers/synonyms); anything it can't resolve to a known object is
either recorded with target_type="unknown"/resolved=False (schema-qualified
but not found in the inventory) or silently skipped (an unqualified name
that matches nothing -- almost always a CTE, derived-table alias, or temp
table/variable, not a real cross-object dependency).
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Iterator

from lakebridge_discovery.logging_setup import logger
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult

_STRIP_BRACKETS = re.compile(r"[\[\]\"`]")
_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)

#  Real exported SQL Server DDL/module text overwhelmingly uses bracket-
# quoted identifiers ("FROM [Sales].[Store]"), not bare names -- the leading
# character class must allow "[" too, or these patterns silently match
# nothing against real files (found by testing against a real Analyzer run's
# exported .sql text, where a bracket-only view definition produced zero
# regex matches until this was fixed).
_READ_PATTERN = re.compile(r"\b(?:FROM|JOIN)\s+([\[A-Za-z_][\w\[\]\.]*)", re.IGNORECASE)
# T-SQL's INSERT statement makes INTO optional ("INSERT dbo.Foo (...)" is
# valid, not just "INSERT INTO dbo.Foo (...)") -- found via a real proc
# (uspLogError) that uses the bare form, which the old INSERT\s+INTO-only
# pattern silently never matched.
_WRITE_PATTERN = re.compile(
    r"\b(?:INSERT(?:\s+INTO)?|UPDATE|DELETE\s+FROM|MERGE(?:\s+INTO)?)\s+([\[A-Za-z_][\w\[\]\.]*)", re.IGNORECASE
)
_CALL_PATTERN = re.compile(r"\bEXEC(?:UTE)?\s+([\[A-Za-z_][\w\[\]\.]*)", re.IGNORECASE)
# A trigger's own "CREATE TRIGGER x ON <table>" clause -- a structural fact
# (which table a trigger fires on), not a code reference, so it gets its own
# relationship_type rather than being folded into reads/writes/calls. Scoped
# to triggers only in extract_dependencies() below.
_FIRES_ON_PATTERN = re.compile(r"\bCREATE\s+TRIGGER\s+[\[\w\].]+\s+ON\s+([\[A-Za-z_][\w\[\]\.]*)", re.IGNORECASE)

# Object categories that can themselves contain referencing SQL text.
_SOURCE_CATEGORIES = ("views", "stored_procedures", "functions", "triggers")
# Every category a reference could resolve to (adds tables/synonyms as pure targets).
_ALL_CATEGORIES = _SOURCE_CATEGORIES + ("tables", "synonyms")


def _clean_sql(text: str) -> str:
    text = _COMMENT_BLOCK.sub(" ", text)
    text = _COMMENT_LINE.sub(" ", text)
    return text


def _normalize_ref(raw: str) -> tuple[str, str | None]:
    """(bare_name, schema) both lowercased; schema is None if unqualified."""
    cleaned = _STRIP_BRACKETS.sub("", raw).strip().rstrip(";,")
    parts = [p for p in cleaned.split(".") if p]
    if not parts:
        return "", None
    if len(parts) == 1:
        return parts[0].lower(), None
    return parts[-1].lower(), parts[-2].lower()


def _build_inventory(result: LakebridgeDiscoveryResult) -> tuple[dict[str, str], dict[str, str]]:
    """qualified: "schema.name" (lower) -> object_type. bare: "name" (lower)
    -> object_type, kept only when unambiguous across the whole inventory."""
    qualified: dict[str, str] = {}
    bare_types: dict[str, set[str]] = {}
    for category in _ALL_CATEGORIES:
        for obj in getattr(result, category):
            if "." not in obj.name:
                continue
            schema, _, bare = obj.name.rpartition(".")
            qualified[f"{schema.lower()}.{bare.lower()}"] = obj.object_type
            bare_types.setdefault(bare.lower(), set()).add(obj.object_type)
    bare = {name: next(iter(types)) for name, types in bare_types.items() if len(types) == 1}
    return qualified, bare


def _build_function_call_pattern(qualified: dict[str, str]) -> re.Pattern | None:
    """Inline scalar/table-valued function calls ("SET @x = [dbo].
    [ufnGetAccountingStartDate]();") aren't EXEC statements, so _CALL_PATTERN
    never sees them -- but matching *any* "identifier(" as a call would be
    extremely false-positive-prone (CAST, COUNT, column expressions, ...).
    Instead this builds a pattern only from names already known to be
    functions in this run's own inventory, so it can only ever match a real,
    named user-defined function -- never a false positive from a generic
    identifier/keyword."""
    functions = [key for key, obj_type in qualified.items() if obj_type == "function"]
    if not functions:
        return None
    alternatives = []
    for key in functions:
        schema, _, name = key.partition(".")
        alternatives.append(rf"\[?{re.escape(schema)}\]?\.\[?{re.escape(name)}\]?")
    return re.compile(r"\b(" + "|".join(alternatives) + r")\s*\(", re.IGNORECASE)


def _resolve(
    bare_name: str, schema: str | None, own_schema: str | None,
    qualified: dict[str, str], bare_index: dict[str, str],
) -> tuple[str | None, str]:
    """Returns (matched "schema.name" or None, object_type)."""
    if schema:
        key = f"{schema}.{bare_name}"
        return (key, qualified[key]) if key in qualified else (None, "unknown")
    if own_schema:
        key = f"{own_schema}.{bare_name}"
        if key in qualified:
            return key, qualified[key]
    if bare_name in bare_index:
        return bare_name, bare_index[bare_name]
    return None, "unknown"


def _scan(
    pattern: re.Pattern, text: str, relationship_type: str, source_name: str, source_type: str,
    own_schema: str | None, qualified: dict[str, str], bare_index: dict[str, str], seen_edges: set[tuple],
) -> Iterator[LakebridgeDependencyRef]:
    for match in pattern.finditer(text):
        bare_name, schema = _normalize_ref(match.group(1))
        if not bare_name:
            continue
        matched_key, target_type = _resolve(bare_name, schema, own_schema, qualified, bare_index)
        if matched_key is None and schema is None:
            continue  # unqualified + unresolved: almost certainly a CTE/alias/temp table, not a real edge
        target_object = matched_key or f"{schema}.{bare_name}"
        if target_object.lower() == source_name.lower():
            continue  # self-loop (e.g. a CREATE FUNCTION header's own name followed by "(" for its parameter
            # list, matching _build_function_call_pattern's own pattern against its own definition) -- not a real edge
        edge_key = (source_name, target_object, relationship_type)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)
        yield LakebridgeDependencyRef(
            source_object=source_name,
            target_object=target_object,
            relationship_type=relationship_type,
            raw_category="text_scan",
            source_type=source_type,
            target_type=target_type,
            discovery_method="lakebridge",
            resolved=matched_key is not None,
        )


def extract_dependencies(result: LakebridgeDiscoveryResult, export_dir: Path) -> None:
    """Appends dependency edges (found by scanning this run's own exported
    SQL definitions) to result.dependencies, and sets result.dependency_stats.
    Never raises -- a missing export dir or unreadable file is recorded as a
    warning, same defensive style as report_parser.py.

    Runs against every source object's text regardless of whether
    report_parser.py's native extraction already covered it -- `seen_edges`
    is pre-seeded from result.dependencies (whatever the native pass already
    found), so this only ever *adds* an edge no prior pass already reported;
    it never duplicates or overrides a native one. See this module's
    docstring for why object-level skipping (the previous behavior) silently
    dropped real edges."""
    seen_edges: set[tuple] = {(d.source_object, d.target_object, d.relationship_type) for d in result.dependencies}

    sql_dir = export_dir / "sql"
    if not sql_dir.is_dir():
        result.warnings.append(f"dependency_extractor: source export dir {sql_dir} not found -- skipping")
        result.dependency_stats = _stats(result.dependencies)
        return

    qualified, bare_index = _build_inventory(result)
    function_call_pattern = _build_function_call_pattern(qualified)
    new_edges: list[LakebridgeDependencyRef] = []

    for category in _SOURCE_CATEGORIES:
        for obj in getattr(result, category):
            name = obj.name
            own_schema = name.split(".", 1)[0].lower() if "." in name else None
            matches = sorted(sql_dir.glob(f"*__{name}.sql"))
            if not matches:
                continue
            try:
                text = _clean_sql(matches[0].read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                result.warnings.append(f"dependency_extractor: could not read {matches[0]}: {exc}")
                continue

            patterns = [
                (_READ_PATTERN, "reads"), (_WRITE_PATTERN, "writes"), (_CALL_PATTERN, "calls"),
            ]
            if category == "triggers":
                patterns.append((_FIRES_ON_PATTERN, "fires_on"))
            if function_call_pattern is not None:
                patterns.append((function_call_pattern, "calls"))

            for pattern, relationship_type in patterns:
                new_edges.extend(
                    _scan(pattern, text, relationship_type, name, obj.object_type, own_schema, qualified, bare_index, seen_edges)
                )

    result.dependencies.extend(new_edges)
    result.dependency_stats = _stats(result.dependencies)
    logger.info(
        "Lakebridge dependency extraction (regex gap-fill): %d new edges found (%d resolved, %d unresolved) "
        "from %d candidate source objects (%d edges already known from the Analyzer report)",
        len(new_edges), sum(1 for e in new_edges if e.resolved), sum(1 for e in new_edges if not e.resolved),
        sum(len(getattr(result, c)) for c in _SOURCE_CATEGORIES), len(seen_edges) - len(new_edges),
    )


def _stats(dependencies: list[LakebridgeDependencyRef]) -> dict:
    by_relationship: Counter = Counter(d.relationship_type for d in dependencies)
    by_type_pair: Counter = Counter(f"{d.source_type}->{d.target_type}" for d in dependencies)
    by_discovery_method: Counter = Counter(d.discovery_method for d in dependencies)
    unique_relationships = {(d.source_object, d.target_object, d.relationship_type) for d in dependencies}
    resolved = sum(1 for d in dependencies if d.resolved)
    return {
        "total_dependencies": len(dependencies),
        "unique_relationships": len(unique_relationships),
        "by_relationship_type": dict(by_relationship),
        "by_type_pair": dict(sorted(by_type_pair.items())),
        "by_discovery_method": dict(by_discovery_method),
        "resolved": resolved,
        "unresolved": len(dependencies) - resolved,
    }
