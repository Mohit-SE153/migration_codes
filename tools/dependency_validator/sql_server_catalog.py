"""
Ground-truth SQL Server catalog queries for the dependency coverage
validator.

Reuses autovista's own connection builder (_connect_live_sql) and, for the
three categories where production already runs exactly the right query
(foreign keys, synonyms, trigger fires-on), the exact same QUERY_*
constants sql_metadata_extractor.py already defines -- so this module
never silently drifts from what production itself treats as ground truth
for those categories.

The one genuinely new query here (expression dependencies) is NOT a
modification of autovista.sql_metadata_extractor.QUERY_EXPRESSION_DEPENDENCIES
-- it selects additional columns production's narrower version has no need
for (referencing_minor_id, referenced_server_name, referenced_database_name,
referenced_minor_id, is_schema_bound_reference, and the referenced object's
own type_desc via a second join to sys.objects), because production only
ever needed OBJECT_OR_COLUMN/TYPE/XML_NAMESPACE-class rows for a narrow
backfill purpose, while this validator needs the full picture to classify
every dependency into MATCHED / MISSING / KNOWN UNSUPPORTED / OUT OF SCOPE.

This module is read-only against SQL Server (SELECT-only queries) and does
not import or call anything that writes to autovista's own output/.
"""
from __future__ import annotations

from dataclasses import dataclass

from autovista.config import AutovistaConfig
from autovista.orchestrator import _connect_live_sql
from autovista.sql_metadata_extractor import (
    QUERY_FOREIGN_KEYS,
    QUERY_SYNONYMS,
    QUERY_TRIGGERS,
)

# Extra columns beyond production's QUERY_EXPRESSION_DEPENDENCIES (see
# module docstring for why): referencing_minor_id, referenced_server_name,
# referenced_database_name, referenced_minor_id, is_schema_bound_reference,
# and a second join to sys.objects to resolve the REFERENCED object's own
# type_desc (only meaningful for OBJECT_OR_COLUMN-class rows -- TYPE/
# XML_NAMESPACE-class referenced_id values live in sys.types/
# sys.xml_schema_collections, different id spaces, so the join simply
# returns NULL there, which is fine since those two classes are already
# unambiguous from referenced_class_desc alone).
QUERY_EXPRESSION_DEPENDENCIES_DETAILED = """
SELECT
    OBJECT_SCHEMA_NAME(sed.referencing_id) AS referencing_schema,
    OBJECT_NAME(sed.referencing_id) AS referencing_name,
    ro.type_desc AS referencing_type,
    sed.referencing_minor_id,
    sed.referenced_server_name,
    sed.referenced_database_name,
    sed.referenced_schema_name,
    sed.referenced_entity_name,
    sed.referenced_class_desc,
    sed.referenced_minor_id,
    sed.is_schema_bound_reference,
    sed.is_ambiguous,
    tgt.type_desc AS referenced_type_desc
FROM sys.sql_expression_dependencies sed
JOIN sys.objects ro ON ro.object_id = sed.referencing_id
LEFT JOIN sys.objects tgt ON tgt.object_id = sed.referenced_id
"""

# Synonyms: production's own QUERY_SYNONYMS doesn't resolve the base
# object's type (dependency_graph_builder.py defers that to
# resolve_target_type() at graph-build time, using already-known
# view/proc/function lists). The validator has no equivalent in-memory
# list to cross-reference against here, so it resolves the type directly
# via a live OBJECT_ID()/sys.objects lookup instead -- a minimal, additive
# variant of production's query, not a modification of it.
QUERY_SYNONYMS_WITH_TYPE = """
SELECT s.name AS schema_name, sy.name AS synonym_name, sy.base_object_name, o.type_desc
FROM sys.synonyms sy
JOIN sys.schemas s ON s.schema_id = sy.schema_id
LEFT JOIN sys.objects o ON o.object_id = OBJECT_ID(sy.base_object_name)
"""


@dataclass
class ExpressionDependencyRow:
    referencing_schema: str
    referencing_name: str
    referencing_type: str
    referencing_minor_id: int
    referenced_server_name: str | None
    referenced_database_name: str | None
    referenced_schema_name: str | None
    referenced_entity_name: str | None
    referenced_class_desc: str
    referenced_minor_id: int
    is_schema_bound_reference: bool
    is_ambiguous: bool
    referenced_type_desc: str | None


@dataclass
class ForeignKeyRow:
    parent_schema: str
    parent_table: str
    ref_schema: str
    ref_table: str


@dataclass
class SynonymRow:
    schema: str
    name: str
    base_object_name: str
    base_object_type_desc: str | None


@dataclass
class TriggerFiresOnRow:
    schema: str
    name: str
    table_name: str


def connect(config: AutovistaConfig):
    """Thin passthrough to autovista's own live connection builder -- kept
    as a named function here (rather than importing _connect_live_sql
    directly at every call site) so the reuse is visible in one place."""
    return _connect_live_sql(config)


def _use_database(connection, database: str) -> None:
    connection.cursor().execute(f"USE [{database}]")


def fetch_expression_dependencies(connection, database: str) -> list[ExpressionDependencyRow]:
    _use_database(connection, database)
    cur = connection.cursor()
    cur.execute(QUERY_EXPRESSION_DEPENDENCIES_DETAILED)
    return [
        ExpressionDependencyRow(
            referencing_schema=r[0], referencing_name=r[1], referencing_type=r[2],
            referencing_minor_id=r[3] or 0,
            referenced_server_name=r[4], referenced_database_name=r[5],
            referenced_schema_name=r[6], referenced_entity_name=r[7],
            referenced_class_desc=r[8], referenced_minor_id=r[9] or 0,
            is_schema_bound_reference=bool(r[10]), is_ambiguous=bool(r[11]),
            referenced_type_desc=r[12],
        )
        for r in cur.fetchall()
    ]


def fetch_foreign_keys(connection, database: str) -> list[ForeignKeyRow]:
    _use_database(connection, database)
    cur = connection.cursor()
    cur.execute(QUERY_FOREIGN_KEYS)
    return [ForeignKeyRow(*row) for row in cur.fetchall()]


def fetch_synonyms(connection, database: str) -> list[SynonymRow]:
    _use_database(connection, database)
    cur = connection.cursor()
    cur.execute(QUERY_SYNONYMS_WITH_TYPE)
    return [SynonymRow(*row) for row in cur.fetchall()]


def fetch_trigger_fires_on(connection, database: str) -> list[TriggerFiresOnRow]:
    """Dedupes to one row per physical trigger (QUERY_TRIGGERS itself
    returns one row per trigger-event via CROSS APPLY sys.trigger_events,
    e.g. 3 rows for one AFTER INSERT, UPDATE, DELETE trigger) -- matching
    production's own dependencies.json, where the dedupe pass in
    dependency_graph_builder.py already collapses a multi-event trigger's
    identical fires_on edge down to one."""
    _use_database(connection, database)
    cur = connection.cursor()
    cur.execute(QUERY_TRIGGERS)
    seen: dict[tuple[str, str], TriggerFiresOnRow] = {}
    for schema_name, trigger_name, table_name, _event, _definition in cur.fetchall():
        seen[(schema_name, trigger_name)] = TriggerFiresOnRow(schema=schema_name, name=trigger_name, table_name=table_name)
    return list(seen.values())
