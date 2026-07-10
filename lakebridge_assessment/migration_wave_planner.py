"""
Dependency-driven migration wave/sequencing plan, built from Lakebridge's
own dependencies[] -- same Tarjan-SCC-then-level algorithm as
assessment/migration_wave_planner.py (independent reimplementation, not
imported -- see schema.py's module docstring), adapted for
case-insensitive object matching (see dependency_index.py for why).
"""
from __future__ import annotations

from lakebridge_assessment.schema import MigrationWave, ObjectComplexity

_SCOPED_CATEGORIES = ("tables", "views", "stored_procedures", "functions", "triggers")


def _known_object_keys(manifest: dict) -> dict[str, str]:
    """lowercase key -> original-cased display name (first occurrence wins)."""
    keys: dict[str, str] = {}
    for field_name in _SCOPED_CATEGORIES:
        for obj in manifest.get(field_name, []):
            name = obj.get("name")
            if name:
                keys.setdefault(name.lower(), name)
    return keys


def _restricted_graph(manifest: dict, known_keys: set[str]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {key: set() for key in known_keys}
    for dep in manifest.get("dependencies", []):
        source, target = dep.get("source_object"), dep.get("target_object")
        if not source or not target:
            continue
        source, target = source.lower(), target.lower()
        if source in known_keys and target in known_keys and source != target:
            graph[source].add(target)
    return graph


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    components: list[list[str]] = []

    def strongconnect(node: str) -> None:
        indices[node] = lowlink[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in graph.get(node, ()):
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif neighbor in on_stack:
                lowlink[node] = min(lowlink[node], indices[neighbor])

        if lowlink[node] == indices[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            components.append(component)

    for node in graph:
        if node not in indices:
            strongconnect(node)
    return components


def _level_condensation(components: list[list[str]], graph: dict[str, set[str]]) -> dict[int, int]:
    node_to_component = {node: i for i, component in enumerate(components) for node in component}
    condensation_edges: list[set[int]] = [set() for _ in components]
    for i, component in enumerate(components):
        for node in component:
            for target in graph.get(node, ()):
                j = node_to_component[target]
                if j != i:
                    condensation_edges[i].add(j)

    levels: dict[int, int] = {}

    def level_of(i: int) -> int:
        if i in levels:
            return levels[i]
        levels[i] = 0 if not condensation_edges[i] else 1 + max(level_of(j) for j in condensation_edges[i])
        return levels[i]

    for i in range(len(components)):
        level_of(i)
    return levels


def build_migration_waves(manifest: dict, object_complexity: list[ObjectComplexity]) -> list[MigrationWave]:
    display_names = _known_object_keys(manifest)
    known_keys = set(display_names)
    if not known_keys:
        return []

    graph = _restricted_graph(manifest, known_keys)
    components = _tarjan_scc(graph)
    levels = _level_condensation(components, graph)

    hours_by_key = {oc.name.lower(): oc.estimated_hours for oc in object_complexity}

    waves_by_level: dict[int, list[str]] = {}
    circular_levels: set[int] = set()
    for i, component in enumerate(components):
        level = levels[i]
        waves_by_level.setdefault(level, []).extend(component)
        if len(component) > 1:
            circular_levels.add(level)

    waves: list[MigrationWave] = []
    for level in sorted(waves_by_level):
        keys = sorted(waves_by_level[level])
        objects = [display_names[k] for k in keys]
        is_circular = level in circular_levels
        rationale = (
            "No prerequisite objects within migration scope -- can migrate first."
            if level == 0 else
            f"All prerequisite objects are migrated in wave {level - 1} or earlier."
        )
        if is_circular:
            rationale += " Contains a circular dependency -- the affected objects must be migrated together as one unit."
        waves.append(MigrationWave(
            wave_number=level, objects=objects, object_count=len(objects),
            estimated_hours=round(sum(hours_by_key.get(k, 0.0) for k in keys), 2),
            rationale=rationale, has_circular_dependency=is_circular,
        ))
    return waves
