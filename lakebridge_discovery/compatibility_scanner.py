"""
SQL-Server-feature compatibility scanner for Lakebridge Discovery.

An INDEPENDENT reimplementation of autovista/compatibility_scanner.py's
detection logic -- never an import of that module, per this codebase's
hard rule that SQLGlot Discovery (autovista/) and Lakebridge Discovery
(lakebridge_discovery/) never share parsing/query logic (see README.md and
dependency_extractor.py's own docstring). Using the `sqlglot` library
directly is fine (it's already a project dependency, and each engine is
free to use it independently) -- importing autovista's own wrapper module
around it is not.

Detects the same named migration-risk construct set autovista's scanner
does (PIVOT/UNPIVOT/CROSS_APPLY/OUTER_APPLY/MERGE/OPENJSON/LINKED_SERVER/
FOR_XML/FOR_JSON/OPENQUERY/OPENDATASOURCE/XP_CMDSHELL/SP_OA), via the same
two-pronged approach:

  - sqlglot AST node types for constructs sqlglot's tsql dialect exposes a
    dedicated node for (MERGE, PIVOT/UNPIVOT, CROSS/OUTER APPLY, OPENJSON,
    4-part linked-server table references).
  - targeted regex for constructs with no dedicated tsql AST node (FOR
    XML/FOR JSON, OPENQUERY/OPENDATASOURCE, xp_cmdshell, sp_OA*), and as a
    fallback whenever a statement fails to parse or degrades to an opaque
    Command node -- the regex scan always runs on the raw text regardless
    of AST success, so a parse failure never blanks out detection.

Scans the SQL text this engine's OWN source_exporter.py already staged on
disk (<source_export_dir>/sql/{kind}__{schema}.{name}.sql), reusing that
module's own file-naming convention (glob-by-suffix, same idiom
dependency_extractor.py's extract_dependencies() already uses to resolve
an object's exported file) -- not a new database query, not new SQL
parsing shared with the other engine.
"""
from __future__ import annotations

import re
from pathlib import Path

import sqlglot
from sqlglot import exp

from lakebridge_discovery.dependency_extractor import _clean_sql
from lakebridge_discovery.logging_setup import logger
from lakebridge_discovery.schema import LakebridgeDiscoveryResult

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
# exactly three dots.
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
# Same two well-documented sqlglot tsql-grammar gaps autovista's scanner
# strips before parsing: an OPTION(...) query-hint clause, and CREATE
# TRIGGER's declaration header (not in the tsql grammar at all -- left
# unstripped, the whole statement degrades to one opaque Command node,
# silently blanking out every AST-based flag for every trigger).
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
    (the regex scan still runs independently on the raw text either way)."""
    flags: set[str] = set()
    try:
        statements = sqlglot.parse(_make_ast_parseable(sql_text), read=dialect)
    except Exception:
        return flags

    for stmt in statements:
        if stmt is None:
            continue
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
    verdict."""
    if not sql_text:
        return []
    flags = _scan_ast_flags(sql_text, dialect=dialect) | _scan_regex_flags(sql_text)
    return sorted(flags)


# Object categories whose exported file this scanner looks for -- same set
# dependency_extractor.py's _SOURCE_CATEGORIES scans for cross-object
# references, plus "tables" (a table has no referencing SQL of its own,
# but its reconstructed DDL can still carry e.g. FOR_XML-shaped computed
# columns or similar, and there's no reason to skip it).
_SCANNED_CATEGORIES = ("tables", "views", "stored_procedures", "functions", "triggers")


def apply_compatibility_flags(result: LakebridgeDiscoveryResult, export_dir: Path) -> None:
    """Sets compatibility_flags on every LakebridgeObjectRef in
    result.tables/views/stored_procedures/functions/triggers, by scanning
    each object's matching exported file at
    <export_dir>/sql/{kind}__{schema}.{name}.sql (source_exporter.py's own
    naming convention). Uses the same glob-by-suffix lookup
    dependency_extractor.py's extract_dependencies() already uses to find
    an object's exported file regardless of its {kind} prefix.

    Never raises: a missing export dir or unreadable file is recorded as a
    warning and that object is simply left with an empty
    compatibility_flags list, mirroring dependency_extractor.py's own
    defensive style."""
    sql_dir = export_dir / "sql"
    if not sql_dir.is_dir():
        result.warnings.append(f"compatibility_scanner: source export dir {sql_dir} not found -- skipping")
        return

    scanned = 0
    for category in _SCANNED_CATEGORIES:
        for obj in getattr(result, category):
            matches = sorted(sql_dir.glob(f"*__{obj.name}.sql"))
            if not matches:
                continue
            try:
                text = matches[0].read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                result.warnings.append(f"compatibility_scanner: could not read {matches[0]}: {exc}")
                continue
            obj.compatibility_flags = scan_compatibility_flags(_clean_sql(text))
            scanned += 1

    logger.info("Compatibility scan: %d objects scanned for migration-risk constructs", scanned)
