"""
Assembles the cross-object dependency graph from everything the other
extractors already resolved. This module does no parsing of its own --
it only combines already-typed results into DependencyEntity edges.

Edge types produced:

  package     -> package        (Execute Package Task)          discovery_method=xml_parsed
  package     -> table/view     (read/write via embedded SQL)    discovery_method=sqlglot | llm_inferred | unresolved
  package     -> proc           (Execute SQL Task calling EXEC)  discovery_method=sqlglot
  proc        -> table/view     (via sql_lineage_parser)         discovery_method=sqlglot | llm_inferred | unresolved
  proc        -> proc           (EXEC inside a proc body)        discovery_method=sqlglot
  proc        -> function       (inline function call)           discovery_method=sqlglot
  view        -> table/view     (view definition)                discovery_method=sqlglot
  view        -> function       (inline function call)           discovery_method=sqlglot
  function    -> table/view     (function body)                  discovery_method=sqlglot
  function    -> function       (nested function call)           discovery_method=sqlglot
  trigger     -> table          (fires_on: the table it's defined ON) discovery_method=direct_metadata
  trigger     -> table/view     (referenced elsewhere in its body) discovery_method=sqlglot
  trigger     -> proc           (EXEC inside a trigger body)      discovery_method=sqlglot
  trigger     -> function       (inline function call)           discovery_method=sqlglot
  table       -> table          (foreign key)                    discovery_method=direct_metadata
  constraint  -> table/view     (CHECK/DEFAULT expression)        discovery_method=sqlglot
  constraint  -> function       (CHECK/DEFAULT expression)        discovery_method=sqlglot
  synonym     -> table/view/proc/function (base object)          discovery_method=direct_metadata
  proc/view/function/trigger/constraint/package -> sequence
                                 (NEXT VALUE FOR)                 discovery_method=sqlglot
  table       -> user_defined_type (column typed as a UDT alias)  discovery_method=direct_metadata
  table       -> xml_schema_collection (XML column binding)       discovery_method=direct_metadata
  table       -> function       (computed column calls a UDF)     discovery_method=sqlglot
  table       -> sequence       (DEFAULT constraint's NEXT VALUE FOR,
                                  companion to the constraint edge below) discovery_method=sqlglot
  function    -> user_defined_type (scalar return type is a UDT alias)  discovery_method=direct_metadata
  <object>    -> table/view/proc/function
                (metadata backfill for objects whose own sqlglot
                 parse degraded/failed -- see build_dependency_graph's
                 expression_dependencies param)                   discovery_method=direct_metadata
  <object>    -> user_defined_type / xml_schema_collection
                (parameter or local-variable typing, from
                 sys.sql_expression_dependencies TYPE/XML_NAMESPACE
                 rows -- unconditional, not gated on parse status,
                 since sqlglot has no way to detect this at all)  discovery_method=direct_metadata

Table vs. view target classification: sqlglot's AST can't tell a table
reference from a view reference apart (both are just a schema-qualified
name to a text parser) -- this module resolves that by cross-checking
each referenced name against the known views list Discovery already
built, so e.g. a proc selecting from a view now gets target_type="view"
rather than a blanket "table" (this refines target_type on some
proc/view/package edges that already existed; relationship_type and
discovery_method for those edges are unchanged). A name that isn't a
known view defaults to "table", matching pre-existing behavior for
anything this distinction doesn't apply to (including names Discovery
never saw at all).

Deduplication: the same edge can legitimately surface from more than one
source (e.g. a trigger's own parent-table "fires_on" edge and a body
reference to that same table both point at the same target) -- the final
list is deduplicated on (source_object, source_type, target_object,
target_type, relationship_type) before being returned.

Not built here, and why:
  - Package -> Function: SSIS embedded-SQL lineage (enrich_embedded_sql)
    isn't extended with function-call detection in this pass -- out of
    scope for a change scoped to SQLGlot dependency discovery on the
    direct-metadata (proc/view/function/trigger/constraint) side; SSIS
    parsing is untouched.
  - Function -> Procedure: not a real SQL Server dependency (T-SQL
    functions can't execute a stored procedure -- no side effects allowed).
  - CLR routine (assembly-backed proc/function) -> Assembly: CLR procs/
    functions are already filtered out of Discovery's object inventory
    entirely (no sys.sql_modules row to join against) -- surfacing this
    needs a new object category, not just a new edge.
  - View/Function/CHECK-constraint/computed-column -> Sequence:
    structurally impossible, not a gap -- NEXT VALUE FOR is rejected by
    SQL Server itself in all four contexts (confirmed empirically: "NEXT
    VALUE FOR function is not allowed in check constraints, default
    objects, computed columns, views, user-defined functions..."). The
    shared sequence-parsing path in sql_lineage_parser.py is still applied
    generically to these (same code as procs/triggers/DEFAULT constraints,
    where it IS valid) -- it will just never find anything there, which is
    correct and requires no special-casing.

sys.sql_expression_dependencies (expression_dependencies param) is SQL
Server's own dependency catalog, covering the same proc/view/function/
trigger/CHECK-constraint scope sqlglot parses, PLUS parameter/local-
variable type usage sqlglot has no way to see at all. Rows are handled
differently by referenced_class_desc:
  - OBJECT_OR_COLUMN: used ONLY to fill in referenced_tables/procs/
    functions for objects whose own sqlglot parse degraded (Command-node
    fallback) or failed outright (unresolved_reason is set) -- never to
    override or second-guess an object sqlglot parsed cleanly.
  - TYPE / XML_NAMESPACE: applied unconditionally (regardless of parse
    status) -- there is no "clean sqlglot parse to second-guess" risk here,
    since sqlglot cannot detect parameter/variable typing under any
    circumstances, gated or not.
Ambiguous rows (is_ambiguous) are always excluded -- never guessed.

This graph is a required output (not optional metadata) -- the
Assessment phase uses it for complexity/blast-radius scoring, so every
edge carries `discovery_method` for confidence weighting and every
object referenced by an edge should already exist as an inventoried
entity (dangling edges to objects Discovery never saw are still emitted,
just not silently dropped, so blast-radius scoring can't undercount).
"""
from __future__ import annotations

from autovista.schema import (
    ConstraintEntity,
    DependencyEntity,
    FunctionEntity,
    PackageEntity,
    StoredProcedureEntity,
    SynonymEntity,
    TableEntity,
    TriggerEntity,
    UserDefinedTypeEntity,
    ViewEntity,
)

# SQL Server's magic trigger-context virtual tables -- not real objects,
# so a metadata-backfill row pointing at them would be misleading. Mirrors
# sql_lineage_parser.py's _TRIGGER_PSEUDO_TABLES (kept as a separate
# constant here to avoid this module importing from the parser module for
# a two-item set).
_PSEUDO_TABLES = {"inserted", "deleted"}

# sys.sql_expression_dependencies referencing_type values this module knows
# how to map to a source_type -- used for both the OBJECT_OR_COLUMN
# backfill and the unconditional TYPE/XML_NAMESPACE edges (see
# _build_expression_dependency_edges). USER_TABLE (computed-column
# expressions, whose referencing_id is the table's own object_id) is
# deliberately excluded -- that category is already covered by direct
# sqlglot parsing of computed_expression (see _type_usage_edges), which
# never degrades the way a full CREATE PROCEDURE/TRIGGER body can, so
# there is no gap to backfill there.
_EXPRESSION_DEPENDENCY_SOURCE_TYPES = {
    "SQL_STORED_PROCEDURE": "stored_procedure",
    "VIEW": "view",
    "SQL_TRIGGER": "trigger",
    "CHECK_CONSTRAINT": "constraint",
    "SQL_SCALAR_FUNCTION": "function",
    "SQL_TABLE_VALUED_FUNCTION": "function",
    "SQL_INLINE_TABLE_VALUED_FUNCTION": "function",
}


def _normalize_key(name: str) -> str:
    return name.strip().strip("[]").lower()


def _table_edges(
    source_object: str, source_type: str, referenced_tables: list[str], discovery_method: str,
    resolve_target_type, relationship_type: str = "reads",
) -> list[DependencyEntity]:
    return [
        DependencyEntity(
            source_object=source_object, source_type=source_type,
            target_object=table, target_type=resolve_target_type(table),
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


def _function_edges(source_object: str, source_type: str, referenced_functions: list[str], discovery_method: str) -> list[DependencyEntity]:
    return [
        DependencyEntity(
            source_object=source_object, source_type=source_type,
            target_object=func, target_type="function",
            relationship_type="calls", discovery_method=discovery_method,
        )
        for func in referenced_functions
    ]


def _sequence_edges(source_object: str, source_type: str, referenced_sequences: list[str], discovery_method: str) -> list[DependencyEntity]:
    return [
        DependencyEntity(
            source_object=source_object, source_type=source_type,
            target_object=seq, target_type="sequence",
            relationship_type="uses_sequence", discovery_method=discovery_method,
        )
        for seq in referenced_sequences
    ]


def _dedupe(dependencies: list[DependencyEntity]) -> list[DependencyEntity]:
    """Keeps the first edge seen for a given (source, target, relationship)
    combination -- e.g. a trigger's fires_on edge and a body reference to
    that same table would otherwise both be emitted."""
    seen: dict[tuple, DependencyEntity] = {}
    for dep in dependencies:
        key = (dep.source_object, dep.source_type, dep.target_object, dep.target_type, dep.relationship_type)
        if key not in seen:
            seen[key] = dep
    return list(seen.values())


def _udt_lookup(user_defined_types: list[UserDefinedTypeEntity]) -> dict[str, str]:
    """Bare (lowercased) UDT name -> "schema.name", shared by every UDT
    bare-name match in this module (table columns, function return types).
    UDT matching is by bare type name only (ColumnEntity.data_type/
    FunctionEntity.return_type are the type's own name with no schema
    prefix, e.g. "PhoneNumber" -- the same "call site never carries a
    schema" limitation _extract_function_calls in sql_lineage_parser.py
    already documents for function calls), cross-referenced against known
    UDT names so built-in system types (varchar, int, ...) are never
    misreported as a dependency."""
    return {udt.name.lower(): f"{udt.schema}.{udt.name}" for udt in user_defined_types}


def _type_usage_edges(
    tables: list[TableEntity], udt_by_bare_name: dict[str, str],
) -> list[DependencyEntity]:
    """table -> user_defined_type / xml_schema_collection / function edges
    derived from already-collected column metadata -- no new parsing pass
    beyond what sql_metadata_extractor.py and the computed-column lineage
    pass (wired in orchestrator.py) already produce."""
    edges: list[DependencyEntity] = []
    for table in tables:
        table_id = f"{table.schema}.{table.name}"
        for column in table.columns:
            udt_target = udt_by_bare_name.get(column.data_type.lower())
            if udt_target:
                edges.append(
                    DependencyEntity(
                        source_object=table_id, source_type="table",
                        target_object=udt_target, target_type="user_defined_type",
                        relationship_type="uses_type", discovery_method="direct_metadata",
                    )
                )
            if column.xml_schema_collection:
                edges.append(
                    DependencyEntity(
                        source_object=table_id, source_type="table",
                        target_object=column.xml_schema_collection, target_type="xml_schema_collection",
                        relationship_type="uses_type", discovery_method="direct_metadata",
                    )
                )
        edges.extend(_function_edges(
            table_id, "table",
            [f for column in table.columns for f in column.referenced_functions],
            "sqlglot",
        ))
    return edges


# referenced_class_desc values this module knows how to act on. Anything
# else (e.g. DATABASE, SCHEMA, ASSEMBLY -- internal/bookkeeping classes
# with no migration value) is silently skipped.
_CLASS_OBJECT_OR_COLUMN = "OBJECT_OR_COLUMN"
_CLASS_TYPE = "TYPE"
_CLASS_XML_NAMESPACE = "XML_NAMESPACE"


def _build_expression_dependency_edges(
    expression_dependencies: list[tuple[str, str, str, str, str, str]],
    stored_procedures: list[StoredProcedureEntity],
    views: list[ViewEntity],
    functions: list[FunctionEntity],
    triggers: list[TriggerEntity],
    constraints: list[ConstraintEntity],
    resolve_target_type,
) -> list[DependencyEntity]:
    """Builds edges from SQL Server's own sys.sql_expression_dependencies
    catalog (see module docstring), handled differently per
    referenced_class_desc:

      OBJECT_OR_COLUMN -- fills gaps for objects whose own sqlglot parse
      degraded or failed. Only ever ADDS edges for objects already flagged
      parse_status=='unresolved' or with a non-null unresolved_reason -- an
      object sqlglot parsed cleanly is never touched here, so this can only
      improve coverage, never override an existing result.

      TYPE / XML_NAMESPACE -- parameter/local-variable typing sqlglot has
      no way to see under any circumstances, so these are applied
      unconditionally (every matching source object, regardless of its own
      parse_status)."""
    needs_backfill: set[tuple[str, str, str]] = set()
    # Constraints are identified elsewhere in this module by the 3-part
    # "schema.table.name" (see the constraints loop in build_dependency_graph)
    # even though sys.sql_expression_dependencies only resolves a
    # constraint's own (schema, name) -- a constraint's name is unique
    # within its schema, so this map recovers the matching 3-part id.
    constraint_source_id: dict[tuple[str, str], str] = {
        (_normalize_key(c.schema), _normalize_key(c.name)): f"{c.schema}.{c.table}.{c.name}"
        for c in constraints
    }
    for source_type, entities in (
        ("stored_procedure", stored_procedures),
        ("view", views),
        ("function", functions),
        ("trigger", triggers),
        ("constraint", constraints),
    ):
        for entity in entities:
            if entity.parse_status == "unresolved" or entity.unresolved_reason is not None:
                needs_backfill.add((source_type, _normalize_key(entity.schema), _normalize_key(entity.name)))

    def source_object_for(source_type: str, norm_schema: str, norm_name: str, ref_schema: str, ref_name: str) -> str:
        if source_type == "constraint":
            return constraint_source_id.get((norm_schema, norm_name), f"{ref_schema}.{ref_name}")
        return f"{ref_schema}.{ref_name}"

    edges: list[DependencyEntity] = []
    for (ref_schema, ref_name, ref_type, target_schema, target_name, target_class) in expression_dependencies:
        source_type = _EXPRESSION_DEPENDENCY_SOURCE_TYPES.get(ref_type)
        if source_type is None:
            continue
        norm_schema, norm_name = _normalize_key(ref_schema), _normalize_key(ref_name)
        source_object = source_object_for(source_type, norm_schema, norm_name, ref_schema, ref_name)
        target_object = f"{target_schema}.{target_name}" if target_schema else target_name

        if target_class == _CLASS_OBJECT_OR_COLUMN:
            if (source_type, norm_schema, norm_name) not in needs_backfill:
                continue
            if _normalize_key(target_name) in _PSEUDO_TABLES:
                continue
            target_type = resolve_target_type(target_object)
            relationship_type = "calls" if target_type in ("stored_procedure", "function") else "reads"
            edges.append(DependencyEntity(
                source_object=source_object, source_type=source_type,
                target_object=target_object, target_type=target_type,
                relationship_type=relationship_type, discovery_method="direct_metadata",
            ))
        elif target_class == _CLASS_TYPE:
            edges.append(DependencyEntity(
                source_object=source_object, source_type=source_type,
                target_object=target_object, target_type="user_defined_type",
                relationship_type="uses_type", discovery_method="direct_metadata",
            ))
        elif target_class == _CLASS_XML_NAMESPACE:
            edges.append(DependencyEntity(
                source_object=source_object, source_type=source_type,
                target_object=target_object, target_type="xml_schema_collection",
                relationship_type="uses_type", discovery_method="direct_metadata",
            ))
    return edges


def build_dependency_graph(
    stored_procedures: list[StoredProcedureEntity],
    views: list[ViewEntity],
    packages: list[PackageEntity],
    foreign_keys: list[tuple[str, str]],
    functions: list[FunctionEntity] | None = None,
    triggers: list[TriggerEntity] | None = None,
    constraints: list[ConstraintEntity] | None = None,
    synonyms: list[SynonymEntity] | None = None,
    tables: list[TableEntity] | None = None,
    user_defined_types: list[UserDefinedTypeEntity] | None = None,
    expression_dependencies: list[tuple[str, str, str, str, str, str]] | None = None,
) -> list[DependencyEntity]:
    functions = functions or []
    triggers = triggers or []
    constraints = constraints or []
    synonyms = synonyms or []
    tables = tables or []
    user_defined_types = user_defined_types or []
    expression_dependencies = expression_dependencies or []

    known_views = {_normalize_key(f"{v.schema}.{v.name}") for v in views}
    known_synonyms = {_normalize_key(f"{s.schema}.{s.name}") for s in synonyms}
    known_procs = {_normalize_key(f"{p.schema}.{p.name}") for p in stored_procedures}
    known_functions = {_normalize_key(f"{f.schema}.{f.name}") for f in functions}

    def resolve_target_type(name: str) -> str:
        key = _normalize_key(name)
        if key in known_views:
            return "view"
        if key in known_synonyms:
            return "synonym"
        if key in known_procs:
            return "stored_procedure"
        if key in known_functions:
            return "function"
        return "table"

    dependencies: list[DependencyEntity] = []

    for proc in stored_procedures:
        proc_id = f"{proc.schema}.{proc.name}"
        dependencies.extend(_table_edges(proc_id, "stored_procedure", proc.referenced_tables, proc.parse_status, resolve_target_type))
        dependencies.extend(_proc_edges(proc_id, "stored_procedure", proc.referenced_procs, proc.parse_status))
        dependencies.extend(_function_edges(proc_id, "stored_procedure", proc.referenced_functions, proc.parse_status))
        dependencies.extend(_sequence_edges(proc_id, "stored_procedure", proc.referenced_sequences, proc.parse_status))

    for view in views:
        view_id = f"{view.schema}.{view.name}"
        dependencies.extend(_table_edges(view_id, "view", view.referenced_tables, view.parse_status, resolve_target_type))
        dependencies.extend(_function_edges(view_id, "view", view.referenced_functions, view.parse_status))
        dependencies.extend(_sequence_edges(view_id, "view", view.referenced_sequences, view.parse_status))

    udt_by_bare_name = _udt_lookup(user_defined_types)

    for func in functions:
        func_id = f"{func.schema}.{func.name}"
        dependencies.extend(_table_edges(func_id, "function", func.referenced_tables, func.parse_status, resolve_target_type))
        dependencies.extend(_function_edges(func_id, "function", func.referenced_functions, func.parse_status))
        dependencies.extend(_sequence_edges(func_id, "function", func.referenced_sequences, func.parse_status))
        # Scalar return type is a UDT alias (e.g. RETURNS dbo.PhoneNumber) --
        # symmetric with _type_usage_edges' table-column UDT match below,
        # same bare-name lookup (a return type is also schema-less at the
        # point sql_metadata_extractor.py fetches it).
        udt_target = udt_by_bare_name.get(func.return_type.lower()) if func.return_type else None
        if udt_target:
            dependencies.append(
                DependencyEntity(
                    source_object=func_id, source_type="function",
                    target_object=udt_target, target_type="user_defined_type",
                    relationship_type="uses_type", discovery_method="direct_metadata",
                )
            )

    for trigger in triggers:
        trigger_id = f"{trigger.schema}.{trigger.name}"
        # The table it's defined ON -- always known (direct_metadata, from
        # sys.triggers), regardless of whether the body itself parsed.
        if trigger.table:
            dependencies.append(
                DependencyEntity(
                    source_object=trigger_id, source_type="trigger",
                    target_object=trigger.table, target_type=resolve_target_type(trigger.table),
                    relationship_type="fires_on", discovery_method="direct_metadata",
                )
            )
        dependencies.extend(_table_edges(trigger_id, "trigger", trigger.referenced_tables, trigger.parse_status, resolve_target_type))
        dependencies.extend(_proc_edges(trigger_id, "trigger", trigger.referenced_procs, trigger.parse_status))
        dependencies.extend(_function_edges(trigger_id, "trigger", trigger.referenced_functions, trigger.parse_status))
        dependencies.extend(_sequence_edges(trigger_id, "trigger", trigger.referenced_sequences, trigger.parse_status))

    for constraint in constraints:
        if not constraint.definition:
            continue  # PRIMARY_KEY/UNIQUE/FOREIGN_KEY -- no expression text to have parsed
        constraint_id = f"{constraint.schema}.{constraint.table}.{constraint.name}"
        dependencies.extend(_table_edges(constraint_id, "constraint", constraint.referenced_tables, constraint.parse_status, resolve_target_type))
        dependencies.extend(_function_edges(constraint_id, "constraint", constraint.referenced_functions, constraint.parse_status))
        dependencies.extend(_sequence_edges(constraint_id, "constraint", constraint.referenced_sequences, constraint.parse_status))
        if constraint.constraint_type == "DEFAULT" and constraint.referenced_sequences:
            # Companion edge naming the table directly -- a DEFAULT
            # constraint's NEXT VALUE FOR is really "this table's column
            # depends on this sequence," which is what deployment-order
            # tooling keys off, not the constraint object itself.
            table_id = f"{constraint.schema}.{constraint.table}"
            dependencies.extend(_sequence_edges(table_id, "table", constraint.referenced_sequences, "sqlglot"))

    for synonym in synonyms:
        if synonym.base_object:
            synonym_id = f"{synonym.schema}.{synonym.name}"
            dependencies.append(
                DependencyEntity(
                    source_object=synonym_id, source_type="synonym",
                    target_object=synonym.base_object, target_type=resolve_target_type(synonym.base_object),
                    relationship_type="references", discovery_method="direct_metadata",
                )
            )

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
                _table_edges(package.name, "package", embedded.referenced_tables, embedded.parse_status, resolve_target_type)
            )
            dependencies.extend(
                _proc_edges(package.name, "package", embedded.referenced_procs, embedded.parse_status)
            )
            dependencies.extend(
                _sequence_edges(package.name, "package", embedded.referenced_sequences, embedded.parse_status)
            )

    dependencies.extend(_type_usage_edges(tables, udt_by_bare_name))
    dependencies.extend(_build_expression_dependency_edges(
        expression_dependencies, stored_procedures, views, functions, triggers, constraints, resolve_target_type,
    ))

    return _dedupe(dependencies)
