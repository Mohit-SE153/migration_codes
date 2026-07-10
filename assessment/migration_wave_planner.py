"""
Dependency-driven migration wave/sequencing plan.

A "wave" is a batch of objects safe to migrate together: every object in
wave N has all of its prerequisites (the objects it reads/calls/references)
already migrated by wave N-1 or earlier. Objects with no prerequisites at
all (e.g. base tables with no outgoing foreign keys) land in wave 0.

Algorithm: restrict the dependency graph to objects actually being
assessed (tables/views/procs/functions/triggers -- the same universe
complexity_scorer.py scores), find strongly connected components (Tarjan's
algorithm) so mutually-dependent objects (e.g. two procs that call each
other) collapse into one unit instead of being incorrectly ordered, then
level the resulting condensation DAG by longest prerequisite chain. All
components at the same level become one wave.

Scale note: uses plain recursion for Tarjan's algorithm, fine at this
project's stated pilot scale (hundreds of objects -- see README.md); an
iterative rewrite would be needed before pointing this at an estate large
enough to risk Python's default recursion limit.
"""
from __future__ import annotations

from assessment.dependency_index import object_key
from assessment.schema import MigrationWave, ObjectComplexity


def _known_object_keys(manifest: dict) -> set[str]:
    keys: set[str] = set()
    for field_name in ("tables", "views", "stored_procedures", "functions", "triggers"):
        for obj in manifest.get(field_name, []):
            keys.add(object_key(obj.get("schema"), obj["name"]))
    return keys


def _restricted_graph(manifest: dict, known_keys: set[str]) -> dict[str, set[str]]:
    """source -> set(targets) it depends on, restricted to edges where both
    ends are in known_keys (edges to sequences/UDTs/XML schema collections/
    unknown objects are prerequisites this planner doesn't sequence, since
    those aren't independently-migrated code/table objects)."""
    graph: dict[str, set[str]] = {key: set() for key in known_keys}
    for dep in manifest.get("dependencies", []):
        source, target = dep.get("source_object"), dep.get("target_object")
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
    """Returns component_index -> level, where level = 0 means no
    prerequisites, and level = 1 + max(prerequisite component levels)
    otherwise. Computed via memoized DFS over the (acyclic) condensation
    graph."""
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
        if not condensation_edges[i]:
            levels[i] = 0
        else:
            levels[i] = 1 + max(level_of(j) for j in condensation_edges[i])
        return levels[i]

    for i in range(len(components)):
        level_of(i)
    return levels


def build_migration_waves(manifest: dict, object_complexity: list[ObjectComplexity]) -> list[MigrationWave]:
    known_keys = _known_object_keys(manifest)
    if not known_keys:
        return []

    graph = _restricted_graph(manifest, known_keys)
    components = _tarjan_scc(graph)
    levels = _level_condensation(components, graph)

    hours_by_key = {oc.name: oc.estimated_hours for oc in object_complexity}

    waves_by_level: dict[int, list[str]] = {}
    circular_levels: set[int] = set()
    for i, component in enumerate(components):
        level = levels[i]
        waves_by_level.setdefault(level, []).extend(component)
        if len(component) > 1:
            circular_levels.add(level)

    waves: list[MigrationWave] = []
    for level in sorted(waves_by_level):
        objects = sorted(waves_by_level[level])
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
            estimated_hours=round(sum(hours_by_key.get(o, 0.0) for o in objects), 2),
            rationale=rationale, has_circular_dependency=is_circular,
        ))
    return waves
