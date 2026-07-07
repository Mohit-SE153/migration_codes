"""
Reads the already-written SQLGlot Discovery output (output/) -- never
writes to it, never imports autovista.orchestrator.run_discovery or
anything that could re-trigger a Discovery run. Production is read-only
from this validator's point of view.

Builds:
  - the raw list of dependency edges already known to SQLGlot
    (output/dependencies.json)
  - a (schema, name) -> "schema.table.name" lookup for constraints, since
    dependencies.json identifies a constraint by its full 3-part id but
    sys.sql_expression_dependencies (and this validator's ground truth)
    only ever resolves a constraint's own (schema, name) -- mirrors the
    exact same constraint_source_id pattern
    autovista.dependency_graph_builder._build_expression_dependency_edges
    already uses for the same reason.
  - a set of (source_type, schema, name) tuples SQLGlot itself flagged as
    dynamic-SQL-unresolved (for Known Unsupported classification) --
    dependencies.json alone can't answer this, since DependencyEntity
    carries no unresolved_reason; only the per-category entity files do.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_DYNAMIC_SQL_MARKER = "dynamic sql"

# (json filename, entity source_type label) -- the entity categories that
# can carry an unresolved_reason at all (matches the set of entities
# enrich_*'d via sql_lineage_parser.py in autovista/orchestrator.py).
_ENTITY_FILES = [
    ("stored_procedures.json", "stored_procedure"),
    ("views.json", "view"),
    ("functions.json", "function"),
    ("triggers.json", "trigger"),
    ("constraints.json", "constraint"),
]


@dataclass
class SqlglotSnapshot:
    dependencies: list[dict]
    dynamic_sql_objects: set[tuple[str, str, str]] = field(default_factory=set)  # (source_type, schema, name)
    constraint_full_id: dict[tuple[str, str], str] = field(default_factory=dict)  # (schema, name) -> "schema.table.name"


def _read_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def load_sqlglot_snapshot(output_dir: str) -> SqlglotSnapshot:
    out = Path(output_dir)
    dependencies = _read_json(out / "dependencies.json")

    dynamic_sql_objects: set[tuple[str, str, str]] = set()
    constraint_full_id: dict[tuple[str, str], str] = {}

    for filename, source_type in _ENTITY_FILES:
        for entity in _read_json(out / filename):
            schema = entity.get("schema")
            name = entity.get("name")
            if source_type == "constraint" and schema and name and entity.get("table"):
                constraint_full_id[(schema.lower(), name.lower())] = f"{schema}.{entity['table']}.{name}"

            reason = (entity.get("unresolved_reason") or "").lower()
            if _DYNAMIC_SQL_MARKER in reason and schema and name:
                dynamic_sql_objects.add((source_type, schema.lower(), name.lower()))

    return SqlglotSnapshot(
        dependencies=dependencies,
        dynamic_sql_objects=dynamic_sql_objects,
        constraint_full_id=constraint_full_id,
    )
