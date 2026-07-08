"""
SQL-Server-feature compatibility scanner (Discovery Enhancement: migration-
risk flags).

Turns "this T-SQL construct is present" into a named, countable signal per
object (compatibility_flags: list[str]) -- constructs SQL Server supports
that have no direct Databricks/Spark SQL equivalent, or need rework during
migration (PIVOT/UNPIVOT, CROSS/OUTER APPLY, MERGE, OPENJSON, FOR XML/FOR
JSON, linked-server references, OLE Automation / xp_cmdshell calls).

This module parses no new SQL of its own -- it scans the SAME definition
text every other extractor already fetched (proc/view/function/trigger
bodies, embedded SQL text), via a mix of:

  - sqlglot AST node types, for constructs sqlglot's tsql dialect exposes
    a dedicated node for -- verified empirically against sqlglot 30.x
    (not assumed from the construct's name alone, per this module's
    build brief):
      * exp.Merge                              -> MERGE
      * exp.Pivot (args["unpivot"] False/None)  -> PIVOT
      * exp.Pivot (args["unpivot"] True)        -> UNPIVOT
      * exp.Lateral (args["cross_apply"] True)  -> CROSS_APPLY
      * exp.Lateral (args["cross_apply"] False) -> OUTER_APPLY
      * exp.OpenJSON                            -> OPENJSON
      * exp.Table with a 4-part dotted name
        (catalog set AND `.this` is an exp.Dot -- the same shape
        sql_lineage_parser.py's _normalize_table_name docstring
        documents for linked-server references)  -> LINKED_SERVER

    NOTE: OPENJSON has its own dedicated sqlglot AST node (confirmed
    empirically), even though migration-risk checklists sometimes lump it
    in with constructs that need regex -- detected via the AST here, not
    regex, since that's strictly more reliable when it's available.

  - targeted regex, for constructs sqlglot's tsql grammar has no
    dedicated AST node for at all (FOR XML / FOR JSON clauses, sp_OA*
    OLE Automation calls, xp_cmdshell, OPENQUERY/OPENDATASOURCE), and as
    a fallback for every flag when the statement degrades to an opaque
    Command node or fails to parse outright (the regex scan always runs
    on the raw text regardless of AST success, mirroring
    sql_lineage_parser.py's Command-node regex supplement). Patterns are
    narrow, keyword-anchored T-SQL matches -- same style as
    sql_lineage_parser.py's _TABLE_REF_IN_COMMAND_TEXT / _NOT_A_TABLE,
    not a blanket keyword search.

Metadata-only, additive: never changes parse_status/referenced_* fields
other extractors already populated -- only ever appends
compatibility_flags. No new database queries -- always fed already-fetched
definition text by the caller (see orchestrator.py).
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

# --- Regex-based flags ---------------------------------------------------
_FOR_XML_PATTERN = re.compile(r"\bFOR\s+XML\b", re.IGNORECASE)
_FOR_JSON_PATTERN = re.compile(r"\bFOR\s+JSON\b", re.IGNORECASE)
_OPENQUERY_PATTERN = re.compile(r"\bOPENQUERY\s*\(", re.IGNORECASE)
_OPENDATASOURCE_PATTERN = re.compile(r"\bOPENDATASOURCE\s*\(", re.IGNORECASE)
_XP_CMDSHELL_PATTERN = re.compile(r"\bxp_cmdshell\b", re.IGNORECASE)
# sp_OACreate / sp_OAMethod / sp_OAGetProperty / sp_OASetProperty /
# sp_OADestroy / sp_OAGetErrorInfo -- SQL Server's OLE Automation stored
# procedures, all sharing the sp_OA prefix.
_SP_OA_PATTERN = re.compile(r"\bsp_OA\w*\b", re.IGNORECASE)
# A 4-part dotted identifier chain (server.database.schema.object) is SQL
# Server's only syntax for a linked-server table reference -- narrowed to
# identifier-like tokens (word chars, optional [] quoting) joined by
# exactly three dots, the same narrow/T-SQL-aware matching style as
# sql_lineage_parser.py's _TABLE_REF_IN_COMMAND_TEXT (not a blanket
# dot-count heuristic that could match unrelated text).
_LINKED_SERVER_4PART_PATTERN = re.compile(
    r"\[?\w+\]?\s*\.\s*\[?\w+\]?\s*\.\s*\[?\w+\]?\s*\.\s*\[?\w+\]?"
)

_REGEX_FLAGS: tuple[tuple[str, re.Pattern], ...] = (
    ("FOR_XML", _FOR_XML_PATTERN),
    ("FOR_JSON", _FOR_JSON_PATTERN),
    ("OPENQUERY", _OPENQUERY_PATTERN),
    ("OPENDATASOURCE", _OPENDATASOURCE_PATTERN),
    ("XP_CMDSHELL", _XP_CMDSHELL_PATTERN),
    ("SP_OA", _SP_OA_PATTERN),
    ("LINKED_SERVER", _LINKED_SERVER_4PART_PATTERN),
)


def _scan_regex_flags(sql_text: str) -> set[str]:
    return {flag for flag, pattern in _REGEX_FLAGS if pattern.search(sql_text)}


# --- AST-parseability preprocessing --------------------------------------
# Mirrors sql_lineage_parser.py's own preprocessing (_strip_unparsed_hints /
# _strip_trigger_declaration) for the same two well-documented sqlglot
# tsql-grammar gaps: an OPTION(...) query-hint clause anywhere in a
# statement, and CREATE TRIGGER's declaration header (not in the tsql
# grammar at all -- the whole statement otherwise degrades to one opaque
# Command node, which would silently blank out every AST-based flag below
# -- MERGE/PIVOT/CROSS_APPLY/OPENJSON/LINKED_SERVER -- for every trigger,
# even though FOR_XML/OPENQUERY/etc. would still fire via the regex scan).
# Duplicated here rather than imported from sql_lineage_parser.py -- same
# "small, self-contained piece, avoid cross-module coupling to another
# module's private helper" call dependency_graph_builder.py already makes
# for _PSEUDO_TABLES/_TRIGGER_PSEUDO_TABLES. Keep in sync if either
# upstream pattern changes.
_OPTION_HINT_CLAUSE = re.compile(r"\bOPTION\s*\((?:[^()]|\([^()]*\))*\)", re.IGNORECASE)
_TRY_CATCH_MARKERS = re.compile(r"\bBEGIN\s+TRY\b|\bEND\s+TRY\b|\bBEGIN\s+CATCH\b|\bEND\s+CATCH\b", re.IGNORECASE)
_TRIGGER_HEADER = re.compile(
    r"^\s*CREATE\s+TRIGGER\s+.*?\bON\b.*?\b(?:AFTER|INSTEAD\s+OF)\b.*?\bAS\b",
    re.IGNORECASE | re.DOTALL,
)
_IS_CREATE_TRIGGER = re.compile(r"^\s*CREATE\s+TRIGGER\b", re.IGNORECASE)
_FIRST_BEGIN = re.compile(r"\bBEGIN\b", re.IGNORECASE)
_LAST_END = re.compile(r"\bEND\b\s*;?\s*$", re.IGNORECASE)


def _make_ast_parseable(sql_text: str) -> str:
    sql_text = _OPTION_HINT_CLAUSE.sub(";", sql_text)
    sql_text = _TRY_CATCH_MARKERS.sub("", sql_text)
    if _IS_CREATE_TRIGGER.match(sql_text):
        sql_text = _TRIGGER_HEADER.sub("", sql_text, count=1)
        sql_text = _FIRST_BEGIN.sub("", sql_text, count=1)
        sql_text = _LAST_END.sub("", sql_text, count=1)
    return sql_text


def _scan_ast_flags(sql_text: str, dialect: str) -> set[str]:
    """Best-effort: a total sqlglot parse failure contributes nothing here
    (the regex scan in scan_compatibility_flags() still runs independently
    on the raw text either way, so a parse failure never blanks out
    detection entirely)."""
    flags: set[str] = set()
    try:
        statements = sqlglot.parse(_make_ast_parseable(sql_text), read=dialect)
    except Exception:
        return flags

    for stmt in statements:
        if stmt is None:
            continue
        # find_all() includes the node itself as well as descendants, so
        # this catches MERGE both as a standalone top-level statement and
        # nested inside a CREATE PROCEDURE/TRIGGER body's Block (the
        # common real-world shape) -- an isinstance(stmt, exp.Merge) check
        # on the top-level statement alone would only ever catch the
        # former.
        if stmt.find(exp.Merge) is not None:
            flags.add("MERGE")
        for pivot in stmt.find_all(exp.Pivot):
            flags.add("UNPIVOT" if pivot.args.get("unpivot") else "PIVOT")
        for lateral in stmt.find_all(exp.Lateral):
            flags.add("CROSS_APPLY" if lateral.args.get("cross_apply") else "OUTER_APPLY")
        if stmt.find(exp.OpenJSON) is not None:
            flags.add("OPENJSON")
        for table in stmt.find_all(exp.Table):
            if table.catalog and isinstance(table.this, exp.Dot):
                flags.add("LINKED_SERVER")
    return flags


def scan_compatibility_flags(sql_text: str | None, dialect: str = "tsql") -> list[str]:
    """Returns a sorted list of named migration-risk flags found in
    `sql_text`. Empty list means none of the specific constructs this
    scanner looks for were detected -- not a general "this SQL is clean"
    verdict (out of scope; see module docstring)."""
    if not sql_text:
        return []
    flags = _scan_ast_flags(sql_text, dialect=dialect) | _scan_regex_flags(sql_text)
    return sorted(flags)
