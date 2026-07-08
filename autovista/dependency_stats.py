"""
Summary statistics over an already-built dependency graph -- independent
reimplementation of lakebridge_discovery.catalog_metadata._compute_stats's
exact output shape (same key names: total_dependencies, unique_relationships,
by_relationship_type, by_type_pair, by_discovery_method, resolved,
unresolved), so the two engines' dependency_stats.json are directly
comparable. Not shared code, and does NOT modify or re-derive
manifest.dependencies itself -- dependency_graph_builder.py remains the
single source of truth for the dependency list; this module only counts.

DependencyEntity has no `resolved` boolean field (unlike Lakebridge's
LakebridgeDependencyRef) -- adding one would be an unrequired schema change
per this task's additive-only constraint. Instead, "resolved" is derived
from discovery_method (a ParseStatus value): "unresolved" is itself one of
the five ParseStatus values a dependency's discovery_method can hold (see
dependency_graph_builder.py for where a degraded/best-effort edge gets that
tag), so "discovery_method != 'unresolved'" is the same real signal without
touching DependencyEntity's shape at all.
"""
from __future__ import annotations

from collections import Counter

from autovista.schema import DependencyEntity

UNRESOLVED_DISCOVERY_METHOD = "unresolved"


def compute_dependency_stats(dependencies: list[DependencyEntity]) -> dict:
    by_relationship: Counter = Counter(d.relationship_type for d in dependencies)
    by_type_pair: Counter = Counter(f"{d.source_type}->{d.target_type}" for d in dependencies)
    by_discovery_method: Counter = Counter(d.discovery_method for d in dependencies)
    unique_relationships = {(d.source_object, d.target_object, d.relationship_type) for d in dependencies}
    resolved = sum(1 for d in dependencies if d.discovery_method != UNRESOLVED_DISCOVERY_METHOD)
    return {
        "total_dependencies": len(dependencies),
        "unique_relationships": len(unique_relationships),
        "by_relationship_type": dict(by_relationship),
        "by_type_pair": dict(sorted(by_type_pair.items())),
        "by_discovery_method": dict(by_discovery_method),
        "resolved": resolved,
        "unresolved": len(dependencies) - resolved,
    }
