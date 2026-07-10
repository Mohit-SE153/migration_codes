"""
Shared adjacency-list view over lakebridge_manifest.json's dependencies[]
edges (LakebridgeDependencyRef.source_object/target_object), used by both
complexity_mapper.py (fan-in/fan-out counts) and migration_wave_planner.py.

Case-insensitive matching, unlike assessment/dependency_index.py's
equivalent: Lakebridge's own report mixes casing between object inventory
names (e.g. "HumanResources.Employee", preserved from sys.* catalog views)
and dependency edge endpoints scraped from the Analyzer report text (e.g.
"humanresources.employee", lowercased) -- confirmed empirically against
this project's own output_lakebridge/lakebridge_manifest.json. Matching
case-sensitively would silently undercount fan-in/fan-out for most
objects, which is worse than the small risk of an unintended collision
from case-folding two distinctly-named objects (SQL Server identifiers are
case-insensitive by default collation anyway, so this isn't a new risk).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class DependencyIndex:
    out_edges: dict[str, set[str]]
    in_edges: dict[str, set[str]]

    def fan_out(self, object_key: str) -> int:
        return len(self.out_edges.get(object_key.lower(), ()))

    def fan_in(self, object_key: str) -> int:
        return len(self.in_edges.get(object_key.lower(), ()))


def build_dependency_index(dependencies: list[dict]) -> DependencyIndex:
    out_edges: dict[str, set[str]] = defaultdict(set)
    in_edges: dict[str, set[str]] = defaultdict(set)
    for dep in dependencies:
        source = dep.get("source_object")
        target = dep.get("target_object")
        if not source or not target:
            continue
        source, target = source.lower(), target.lower()
        if source == target:
            continue
        out_edges[source].add(target)
        in_edges[target].add(source)
    return DependencyIndex(out_edges=dict(out_edges), in_edges=dict(in_edges))
