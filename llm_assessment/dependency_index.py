"""
Shared adjacency-list view over manifest["dependencies"] edges. Fully
self-contained independent copy (not imported from assessment/) -- see
schema.py's module docstring for why. Case-sensitive matching is correct
here (unlike lakebridge_assessment's equivalent): this package reads the
sqlglot Discovery manifest, whose DependencyEntity.source_object/
target_object strings consistently match the object inventory's own
naming (no cross-casing inconsistency like Lakebridge's report has).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class DependencyIndex:
    out_edges: dict[str, set[str]]
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
