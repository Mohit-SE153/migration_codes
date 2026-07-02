"""
Assembles the cross-object dependency graph from everything the other
extractors already resolved. This module does no parsing of its own --
it only combines already-typed results into DependencyEntity edges. Edge
types produced:

  package -> package        (Execute Package Task)          discovery_method=xml_parsed
  package -> table/view     (read/write via embedded SQL)    discovery_method=sqlglot | llm_inferred | unresolved
  proc    -> table          (via sql_lineage_parser)         discovery_method=sqlglot | llm_inferred | unresolved
  proc    -> proc           (EXEC inside a proc body)        discovery_method=sqlglot
  package -> proc           (Execute SQL Task calling EXEC)  discovery_method=sqlglot
  table   -> table          (foreign key)                    discovery_method=direct_metadata
  view    -> table          (view definition)                discovery_method=sqlglot

This graph is a required output (not optional metadata) -- the
Assessment phase uses it for complexity/blast-radius scoring, so every
edge carries `discovery_method` for confidence weighting and every
object referenced by an edge should already exist as an inventoried
entity (dangling edges to objects Discovery never saw are still emitted,
just not silently dropped, so blast-radius scoring can't undercount).
"""
from __future__ import annotations

from autovista.schema import (
    DependencyEntity,
    PackageEntity,
    StoredProcedureEntity,
    ViewEntity,
)


def _table_edges(source_object: str, source_type: str, referenced_tables: list[str], discovery_method: str, relationship_type: str = "reads") -> list[DependencyEntity]:
    return [
        DependencyEntity(
            source_object=source_object, source_type=source_type,
            target_object=table, target_type="table",
            relationship_type=relationship_type, discovery_method=discovery_method,
        )
        for table in referenced_tables
    ]


def _proc_edges(source_object: str, source_type: str, referenced_procs: list[str], discovery_method: str) -> list[DependencyEntity]:
    return [
        DependencyEntity(
            source_object=source_object, source_type=source_type,
            target_object=proc, target_type="stored_procedure",
            relationship_type="calls", discovery_method=discovery_method,
        )
        for proc in referenced_procs
    ]


def build_dependency_graph(
    stored_procedures: list[StoredProcedureEntity],
    views: list[ViewEntity],
    packages: list[PackageEntity],
    foreign_keys: list[tuple[str, str]],
) -> list[DependencyEntity]:
    dependencies: list[DependencyEntity] = []

    for proc in stored_procedures:
        proc_id = f"{proc.schema}.{proc.name}"
        dependencies.extend(_table_edges(proc_id, "stored_procedure", proc.referenced_tables, proc.parse_status))
        dependencies.extend(_proc_edges(proc_id, "stored_procedure", proc.referenced_procs, proc.parse_status))

    for view in views:
        view_id = f"{view.schema}.{view.name}"
        dependencies.extend(_table_edges(view_id, "view", view.referenced_tables, view.parse_status))

    for from_table, to_table in foreign_keys:
        dependencies.append(
            DependencyEntity(
                source_object=from_table, source_type="table",
                target_object=to_table, target_type="table",
                relationship_type="foreign_key", discovery_method="direct_metadata",
            )
        )

    for package in packages:
        for task in package.tasks:
            if task.executed_package:
                dependencies.append(
                    DependencyEntity(
                        source_object=package.name, source_type="package",
                        target_object=task.executed_package, target_type="package",
                        relationship_type="executes", discovery_method="xml_parsed",
                    )
                )

        for embedded in package.embedded_sql:
            dependencies.extend(
                _table_edges(package.name, "package", embedded.referenced_tables, embedded.parse_status)
            )
            dependencies.extend(
                _proc_edges(package.name, "package", embedded.referenced_procs, embedded.parse_status)
            )

    return dependencies
