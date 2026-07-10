"""
Shared adjacency-list view over Discovery's manifest["dependencies"] edges,
used by both complexity_scorer.py (fan-in/fan-out counts) and
migration_wave_planner.py (topological leveling). Built once per run and
passed in, rather than each caller re-scanning the dependency list.

Object identity: "schema.name" (e.g. "dbo.Orders"), matching exactly how
Discovery's own DependencyEntity.source_object/target_object strings are
formatted (see autovista/dependency_graph_builder.py) -- DependencyEntity
has no `database` field, so this can only disambiguate within a single
database, same limitation Discovery's own dependency graph already has
(one database per Discovery run -- see autovista/orchestrator.py). Not a
new gap introduced here.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class DependencyIndex:
    # object_key -> set of object_keys it depends on / references
    out_edges: dict[str, set[str]]
    # object_key -> set of object_keys that depend on / reference it
    in_edges: dict[str, set[str]]

    def fan_out(self, object_key: str) -> int:
        return len(self.out_edges.get(object_key, ()))

    def fan_in(self, object_key: str) -> int:
        return len(self.in_edges.get(object_key, ()))


def object_key(schema: str | None, name: str) -> str:
    return f"{schema}.{name}" if schema else name


def build_dependency_index(dependencies: list[dict]) -> DependencyIndex:
    out_edges: dict[str, set[str]] = defaultdict(set)
    in_edges: dict[str, set[str]] = defaultdict(set)
    for dep in dependencies:
        source = dep.get("source_object")
        target = dep.get("target_object")
        if not source or not target or source == target:
            continue
        out_edges[source].add(target)
        in_edges[target].add(source)
    return DependencyIndex(out_edges=dict(out_edges), in_edges=dict(in_edges))
