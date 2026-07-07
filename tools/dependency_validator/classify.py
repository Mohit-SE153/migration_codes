"""
Classifies every SQL Server ground-truth dependency into exactly one of
four categories:

  MATCHED                    -- already present in output/dependencies.json
  MISSING - MIGRATION RELEVANT -- absent, and migration-planning cares about it
  KNOWN UNSUPPORTED          -- absent because the source object's own
                                 SQLGlot parse was flagged unresolved due to
                                 dynamic SQL (a known, documented limitation,
                                 not a silent gap)
  OUT OF SCOPE               -- column-level, ambiguous, or internal SQL
                                 Server bookkeeping with no migration value

A MATCHED row is additionally flagged representation_difference=True when
it only matched after stripping a redundant home-database prefix (see
normalize.strict_and_loose_keys) -- still MATCHED, just reported separately
so it's never confused with a real gap.

Reuses autovista.dependency_graph_builder's own source-type vocabulary
(_EXPRESSION_DEPENDENCY_SOURCE_TYPES) as the base of REFERENCING_TYPE_MAP
below, so this validator's classification can never silently drift from
what production itself considers a "stored_procedure"/"view"/etc. source
type. Extended locally (read-only import, nothing in autovista/ is
touched) with the handful of type_desc values production's narrower
backfill mechanism has no need for: DEFAULT_CONSTRAINT, USER_TABLE.
"""
from __future__ import annotations

from dataclasses import dataclass

from autovista.dependency_graph_builder import _EXPRESSION_DEPENDENCY_SOURCE_TYPES, _normalize_key
from tools.dependency_validator.normalize import strict_and_loose_keys
from tools.dependency_validator.sql_server_catalog import (
    ExpressionDependencyRow,
    ForeignKeyRow,
    SynonymRow,
    TriggerFiresOnRow,
)

MATCHED = "MATCHED"
MISSING = "MISSING - MIGRATION RELEVANT"
KNOWN_UNSUPPORTED = "KNOWN UNSUPPORTED"
OUT_OF_SCOPE = "OUT OF SCOPE"

REFERENCING_TYPE_MAP: dict[str, str] = {
    **_EXPRESSION_DEPENDENCY_SOURCE_TYPES,
    "DEFAULT_CONSTRAINT": "constraint",
    "USER_TABLE": "table",  # computed-column expressions: referencing_id is the table's own object_id
}

# The referenced object's own type_desc (from sys.objects, OBJECT_OR_COLUMN
# class rows only -- TYPE/XML_NAMESPACE are already unambiguous from
# referenced_class_desc alone) -> this project's target_type vocabulary.
REFERENCED_TYPE_MAP: dict[str, str] = {
    "USER_TABLE": "table",
    "VIEW": "view",
    "SQL_STORED_PROCEDURE": "stored_procedure",
    "SQL_SCALAR_FUNCTION": "function",
    "SQL_TABLE_VALUED_FUNCTION": "function",
    "SQL_INLINE_TABLE_VALUED_FUNCTION": "function",
    "SEQUENCE_OBJECT": "sequence",
    "SYNONYM": "synonym",
}

_PSEUDO_TABLES = {"inserted", "deleted"}
_RELEVANT_CLASSES = frozenset({"OBJECT_OR_COLUMN", "TYPE", "XML_NAMESPACE"})


@dataclass
class ClassifiedDependency:
    category: str
    source_object: str
    source_type: str
    target_object: str
    target_type: str
    relationship_type: str
    reason: str = ""
    representation_difference: bool = False


def build_sqlglot_key_sets(dependencies: list[dict], home_database: str) -> tuple[set[tuple], set[tuple]]:
    """One (source, source_type, target, target_type, relationship) key set
    at strict granularity and one at loose (home-database-prefix-agnostic)
    granularity, built once from output/dependencies.json and reused for
    every ground-truth row's lookup."""
    strict_keys: set[tuple] = set()
    loose_keys: set[tuple] = set()
    for dep in dependencies:
        src_strict, src_loose = strict_and_loose_keys(dep["source_object"], home_database)
        tgt_strict, tgt_loose = strict_and_loose_keys(dep["target_object"], home_database)
        rel = dep["relationship_type"].lower()
        strict_keys.add((src_strict, dep["source_type"], tgt_strict, dep["target_type"], rel))
        loose_keys.add((src_loose, dep["source_type"], tgt_loose, dep["target_type"], rel))
    return strict_keys, loose_keys


def _relationship_for(target_type: str) -> str:
    if target_type in ("stored_procedure", "function"):
        return "calls"
    if target_type == "sequence":
        return "uses_sequence"
    if target_type in ("user_defined_type", "xml_schema_collection"):
        return "uses_type"
    return "reads"


def _match_or_missing(
    source_object: str, source_type: str, target_object: str, target_type: str, relationship_type: str,
    sqlglot_keys_strict: set[tuple], sqlglot_keys_loose: set[tuple], home_database: str,
) -> ClassifiedDependency:
    src_strict, src_loose = strict_and_loose_keys(source_object, home_database)
    tgt_strict, tgt_loose = strict_and_loose_keys(target_object, home_database)
    rel = relationship_type.lower()

    if (src_strict, source_type, tgt_strict, target_type, rel) in sqlglot_keys_strict:
        return ClassifiedDependency(MATCHED, source_object, source_type, target_object, target_type, relationship_type)
    if (src_loose, source_type, tgt_loose, target_type, rel) in sqlglot_keys_loose:
        return ClassifiedDependency(
            MATCHED, source_object, source_type, target_object, target_type, relationship_type,
            representation_difference=True,
        )
    return ClassifiedDependency(MISSING, source_object, source_type, target_object, target_type, relationship_type)


def _constraint_source_object(schema: str, name: str, constraint_full_id: dict[tuple[str, str], str]) -> str:
    return constraint_full_id.get((_normalize_key(schema), _normalize_key(name)), f"{schema}.{name}")


def _source_object(source_type: str, schema: str, name: str, constraint_full_id: dict[tuple[str, str], str]) -> str:
    if source_type == "constraint":
        return _constraint_source_object(schema, name, constraint_full_id)
    return f"{schema}.{name}"


def classify_expression_dependency(
    row: ExpressionDependencyRow,
    constraint_full_id: dict[tuple[str, str], str],
    dynamic_sql_objects: set[tuple[str, str, str]],
    sqlglot_keys_strict: set[tuple],
    sqlglot_keys_loose: set[tuple],
    home_database: str,
) -> ClassifiedDependency:
    source_type = REFERENCING_TYPE_MAP.get(row.referencing_type)
    if source_type is None:
        return ClassifiedDependency(
            category=OUT_OF_SCOPE,
            source_object=f"{row.referencing_schema}.{row.referencing_name}",
            source_type=row.referencing_type.lower(),
            target_object=row.referenced_entity_name or "", target_type="",
            relationship_type="",
            reason=f"referencing object type '{row.referencing_type}' is not modeled by this project",
        )

    source_object = _source_object(source_type, row.referencing_schema, row.referencing_name, constraint_full_id)

    if row.is_ambiguous:
        return ClassifiedDependency(
            category=OUT_OF_SCOPE, source_object=source_object, source_type=source_type,
            target_object=row.referenced_entity_name or "", target_type="",
            relationship_type="", reason="ambiguous reference -- SQL Server itself could not resolve the target uniquely",
        )

    if row.referenced_minor_id > 0 or row.referenced_class_desc not in _RELEVANT_CLASSES:
        reason = (
            "column-level reference (referenced_minor_id > 0)" if row.referenced_minor_id > 0
            else f"internal metadata class ({row.referenced_class_desc}), no migration-planning value"
        )
        return ClassifiedDependency(
            category=OUT_OF_SCOPE, source_object=source_object, source_type=source_type,
            target_object=row.referenced_entity_name or "", target_type=(row.referenced_class_desc or "").lower(),
            relationship_type="", reason=reason,
        )

    if row.referenced_entity_name and row.referenced_entity_name.lower() in _PSEUDO_TABLES:
        return ClassifiedDependency(
            category=OUT_OF_SCOPE, source_object=source_object, source_type=source_type,
            target_object=row.referenced_entity_name, target_type="pseudo_table", relationship_type="",
            reason="trigger-context virtual table (inserted/deleted), not a real database object",
        )

    if row.referenced_class_desc == "TYPE":
        target_type = "user_defined_type"
    elif row.referenced_class_desc == "XML_NAMESPACE":
        target_type = "xml_schema_collection"
    else:
        target_type = REFERENCED_TYPE_MAP.get(row.referenced_type_desc or "", "table")

    target_parts = [p for p in (
        row.referenced_server_name, row.referenced_database_name, row.referenced_schema_name, row.referenced_entity_name,
    ) if p]
    target_object = ".".join(target_parts)
    relationship_type = _relationship_for(target_type)

    result = _match_or_missing(
        source_object, source_type, target_object, target_type, relationship_type,
        sqlglot_keys_strict, sqlglot_keys_loose, home_database,
    )
    if result.category == MATCHED:
        return result

    # A real match always wins over the dynamic-SQL gate -- in practice
    # these two never coincide (DYNAMIC_SQL_MARKERS short-circuits
    # parse_lineage() to an empty referenced_tables/procs before any
    # parsing is attempted, so a dynamic-SQL-flagged object never has ANY
    # sqlglot-derived edges to match against), but checking the match
    # first rather than gating unconditionally keeps that guarantee
    # explicit in the code rather than assumed.
    dynamic_key = (source_type, _normalize_key(row.referencing_schema), _normalize_key(row.referencing_name))
    if dynamic_key in dynamic_sql_objects:
        return ClassifiedDependency(
            category=KNOWN_UNSUPPORTED, source_object=source_object, source_type=source_type,
            target_object=target_object, target_type=target_type, relationship_type=relationship_type,
            reason="referencing object's own SQLGlot parse was flagged unresolved due to dynamic SQL",
        )
    return result


def classify_foreign_key(
    row: ForeignKeyRow, sqlglot_keys_strict: set[tuple], sqlglot_keys_loose: set[tuple], home_database: str,
) -> ClassifiedDependency:
    source_object = f"{row.parent_schema}.{row.parent_table}"
    target_object = f"{row.ref_schema}.{row.ref_table}"
    return _match_or_missing(
        source_object, "table", target_object, "table", "foreign_key",
        sqlglot_keys_strict, sqlglot_keys_loose, home_database,
    )


def classify_synonym(
    row: SynonymRow, sqlglot_keys_strict: set[tuple], sqlglot_keys_loose: set[tuple], home_database: str,
) -> ClassifiedDependency:
    source_object = f"{row.schema}.{row.name}"
    target_object = row.base_object_name
    target_type = REFERENCED_TYPE_MAP.get(row.base_object_type_desc or "", "table")
    return _match_or_missing(
        source_object, "synonym", target_object, target_type, "references",
        sqlglot_keys_strict, sqlglot_keys_loose, home_database,
    )


def classify_trigger_fires_on(
    row: TriggerFiresOnRow, sqlglot_keys_strict: set[tuple], sqlglot_keys_loose: set[tuple], home_database: str,
) -> ClassifiedDependency:
    """target_object is deliberately the bare table name (no schema
    prefix) -- mirrors production's own TriggerEntity.table /
    OBJECT_NAME(tr.parent_id) convention exactly (confirmed against real
    dependencies.json output), so this compares like-for-like rather than
    "improving" on a pre-existing production quirk this task must not
    touch."""
    source_object = f"{row.schema}.{row.name}"
    return _match_or_missing(
        source_object, "trigger", row.table_name, "table", "fires_on",
        sqlglot_keys_strict, sqlglot_keys_loose, home_database,
    )
