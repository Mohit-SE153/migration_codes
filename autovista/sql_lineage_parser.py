"""
T-SQL lineage parsing via sqlglot: extracts table and stored-procedure
references out of stored procedure bodies, view definitions, and
embedded SQL text pulled from SSIS tasks (Execute SQL Task,
OLE DB Source/Destination, Lookup, OLE DB Command).

sqlglot is a text-in/AST-out SQL parser -- it has no concept of .dtsx
XML, SSIS control flow, or connection managers. Its ONLY job here is:
given a string of T-SQL, return the tables/procs it references, or
report that it couldn't (dynamic SQL, unsupported syntax) so the caller
can route to the LLM fallback instead of guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from autovista.schema import EmbeddedSqlEntity, StoredProcedureEntity, ViewEntity

# Statements that indicate the SQL text builds/executes a string at
# runtime -- sqlglot can parse the wrapper statement but cannot resolve
# what table names end up inside the dynamic string. Anything matching
# this is always routed to unresolved/LLM fallback, never guessed.
DYNAMIC_SQL_MARKERS = re.compile(r"\bsp_executesql\b|\bEXEC(?:UTE)?\s*\(\s*@", re.IGNORECASE)

# `OPTION (query hint [, ...])` -- e.g. `OPTION (MAXRECURSION 25)` -- is a
# legal, common T-SQL query-hint clause that sqlglot's tsql grammar does
# not parse, and it has zero bearing on lineage (no table/column
# references live inside it). Found via live validation against a real
# AdventureWorks2022 instance: `uspGetBillOfMaterials`, `uspGetWhereUsedProductID`,
# `uspGetEmployeeManagers`, and `uspGetManagerEmployees` all use recursive
# CTEs terminated by OPTION (MAXRECURSION N) and were incorrectly falling
# back to `unresolved` because of this single clause. Replaced with `;`
# rather than stripped to empty, so the statement it terminates stays
# explicitly closed -- without a semicolon there, sqlglot's tsql grammar
# silently swallows the preceding CTE/SELECT into an opaque Command node
# instead of raising, which would produce a *worse* failure mode: a
# confident `parse_status: sqlglot` with an incorrectly empty table list.
# Handles one level of nested parens (e.g. `OPTION (OPTIMIZE FOR (@x = 1))`).
_OPTION_HINT_CLAUSE = re.compile(r"\bOPTION\s*\((?:[^()]|\([^()]*\))*\)", re.IGNORECASE)

# BEGIN TRY/END TRY/BEGIN CATCH/END CATCH: sqlglot's tsql grammar has no
# concept of TRY/CATCH at all -- `BEGIN TRY` misparses as a bogus column
# alias, and the surrounding CREATE PROCEDURE body silently degrades to
# opaque Command nodes for everything from that point on, dropping every
# table reference inside the TRY and CATCH bodies with NO error raised
# (a worse failure mode than `unresolved`: a confident, silently wrong
# empty/truncated result). Found via live validation against a real
# AdventureWorks2022 instance (`uspLogError`, `uspUpdateEmployeeLogin`,
# `uspUpdateEmployeePersonalInfo`, `uspPrintError` all use TRY/CATCH and
# were returning empty referenced_tables with no indication of failure).
#
# Fix: strip the TRY/CATCH markers entirely (not replace with BEGIN/END --
# tried that first; sqlglot's Block parser also only correctly handles one
# level of *nested* BEGIN/END, so a second sibling BEGIN/END block still
# breaks). TRY/CATCH is pure error-handling control flow with no bearing
# on which tables/procs are referenced somewhere in the body, so flattening
# both bodies into the outer BEGIN...END is safe for lineage-extraction
# purposes specifically (this is never executed, only read for references).
_TRY_CATCH_MARKERS = re.compile(r"\bBEGIN\s+TRY\b|\bEND\s+TRY\b|\bBEGIN\s+CATCH\b|\bEND\s+CATCH\b", re.IGNORECASE)


def _strip_unparsed_hints(sql_text: str) -> str:
    sql_text = _OPTION_HINT_CLAUSE.sub(";", sql_text)
    sql_text = _TRY_CATCH_MARKERS.sub("", sql_text)
    return sql_text


# Best-effort regex fallback for table references inside opaque Command
# nodes (raw text sqlglot couldn't parse into a real AST -- e.g. deeper
# nested BEGIN/END than its tsql grammar supports). Deliberately narrow:
# only the handful of keywords that reliably precede a table name. This
# is a supplement to, not a replacement for, AST-based extraction --
# results from this path always carry the `saw_unsupported` caveat below,
# since raw-text regex matching can't reason about scope/aliases the way
# the AST walk above does.
_TABLE_REF_IN_COMMAND_TEXT = re.compile(
    r"\b(?:FROM|JOIN|INSERT(?:\s+INTO)?|UPDATE|DELETE(?:\s+FROM)?)\s+\[?(\w+)\]?(?:\s*\.\s*\[?(\w+)\]?)?",
    re.IGNORECASE,
)

# Table-valued functions and other keywords that can follow FROM/JOIN but
# are not table names -- excluded so the regex fallback doesn't report
# e.g. "CONTAINSTABLE" as a referenced table for a full-text search call
# like `FROM CONTAINSTABLE(HumanResources.JobCandidate, *, @query)`.
_NOT_A_TABLE = {
    "SELECT", "VALUES", "CONTAINSTABLE", "FREETEXTTABLE", "OPENROWSET",
    "OPENQUERY", "OPENXML", "OPENDATASOURCE", "OPENJSON",
}


def _extract_table_like_refs(text: str) -> set[str]:
    refs = set()
    for first, second in _TABLE_REF_IN_COMMAND_TEXT.findall(text):
        if first.upper() in _NOT_A_TABLE:
            continue
        refs.add(f"{first}.{second}" if second else first)
    return refs


@dataclass
class LineageResult:
    referenced_tables: list[str]
    referenced_procs: list[str]
    parse_status: str  # "sqlglot" | "unresolved"
    unresolved_reason: str | None = None


def _normalize_table_name(table: exp.Table) -> str:
    parts = [p for p in (table.db, table.name) if p]
    return ".".join(parts) if parts else table.name


def _collect_alias_names(root: exp.Expression) -> set[str]:
    """Table aliases (e.g. the `i` in `FROM dbo.Inventory i`) so bare,
    unaliased references to them elsewhere in the statement (e.g.
    `UPDATE i SET ...`) aren't mistaken for standalone table objects."""
    return {ta.this.name for ta in root.find_all(exp.TableAlias) if ta.this}


def _collect_cte_names(root: exp.Expression) -> set[str]:
    """CTE names (e.g. `BOM_cte` in `WITH BOM_cte AS (...)`). Unlike table
    aliases, a CTE name must be excluded even when the reference to it
    carries its own fresh alias -- recursive CTEs commonly self-reference
    with one (`FROM [BOM_cte] cte`), which would otherwise be misread as
    a real table named "BOM_cte"."""
    return {cte.alias_or_name for cte in root.find_all(exp.CTE)}


def parse_lineage(sql_text: str, dialect: str = "tsql") -> LineageResult:
    if DYNAMIC_SQL_MARKERS.search(sql_text):
        return LineageResult(
            referenced_tables=[], referenced_procs=[], parse_status="unresolved",
            unresolved_reason="dynamic SQL (sp_executesql / EXEC(@var)) -- table names not statically resolvable",
        )

    sql_text = _strip_unparsed_hints(sql_text)

    try:
        statements = sqlglot.parse(sql_text, read=dialect)
    except Exception as exc:
        return LineageResult(
            referenced_tables=[], referenced_procs=[], parse_status="unresolved",
            unresolved_reason=f"sqlglot parse error: {exc}",
        )

    tables: set[str] = set()
    procs: set[str] = set()
    saw_unsupported = False

    for stmt in statements:
        if stmt is None:
            saw_unsupported = True
            continue

        # For CREATE PROCEDURE/VIEW, only walk the body (`expression`) --
        # otherwise the object's own name (in `this`/`this.this`) gets
        # picked up as if it referenced itself.
        body = stmt.args.get("expression") if isinstance(stmt, exp.Create) else stmt
        if body is None:
            continue

        alias_names = _collect_alias_names(body)
        cte_names = _collect_cte_names(body)

        # EXEC/EXECUTE statements parse into a dedicated Execute node
        # whose `.this` is the called proc's Table -- collect those first
        # so the generic Table walk below doesn't misclassify a proc call
        # as a table reference.
        execute_targets: set[int] = set()
        for ex in body.find_all(exp.Execute):
            if isinstance(ex.this, exp.Table):
                execute_targets.add(id(ex.this))
                procs.add(_normalize_table_name(ex.this))

        for table in body.find_all(exp.Table):
            if not table.name or id(table) in execute_targets:
                continue
            if table.db:
                tables.add(_normalize_table_name(table))
                continue
            # Unqualified reference: could be a real unqualified table, a
            # bare alias reference (`UPDATE i SET ...`), or a CTE
            # self-reference (aliased or not) -- only the first is a real
            # table.
            if table.name in cte_names:
                continue
            is_bare_alias_ref = table.name in alias_names and not table.args.get("alias")
            if is_bare_alias_ref:
                continue
            tables.add(_normalize_table_name(table))

        # Any Command node means part of the body wasn't AST-parsed --
        # regex-supplement what we can (EXEC calls, then generic table
        # refs) and flag the whole result as needing a human/LLM second
        # look, since raw-text matching can silently miss references an
        # AST walk would have caught. Found via live validation: nested
        # IF/BEGIN/END inside a TRY block (uspLogError) and full-text
        # search predicates (uspSearchCandidateResumes) both degrade to
        # Command nodes on real AdventureWorks2022 procs.
        for cmd_node in body.find_all(exp.Command):
            saw_unsupported = True
            text = cmd_node.sql(dialect=dialect)
            m = re.search(r"EXEC(?:UTE)?\s+(?:\[?(\w+)\]?\.)?\[?(\w+)\]?", text, re.IGNORECASE)
            if m:
                schema, proc = m.groups()
                procs.add(f"{schema or 'dbo'}.{proc}")
            tables.update(_extract_table_like_refs(text))

    return LineageResult(
        referenced_tables=sorted(tables),
        referenced_procs=sorted(procs),
        parse_status="sqlglot",
        unresolved_reason=(
            "sqlglot fell back to a generic Command node for part of this statement -- "
            "referenced_tables/referenced_procs may be incomplete; verify manually"
        ) if saw_unsupported else None,
    )


def enrich_stored_procedure(proc: StoredProcedureEntity, definition: str) -> StoredProcedureEntity:
    """Fills in referenced_tables/referenced_procs on a StoredProcedureEntity
    whose existence/loc were already established via direct_metadata."""
    result = parse_lineage(definition)
    proc.referenced_tables = result.referenced_tables
    proc.referenced_procs = result.referenced_procs
    proc.parse_status = result.parse_status
    proc.unresolved_reason = result.unresolved_reason
    return proc


def build_view_entity(database: str, schema: str, name: str, definition: str) -> ViewEntity:
    result = parse_lineage(definition)
    return ViewEntity(
        database=database, schema=schema, name=name,
        referenced_tables=result.referenced_tables,
        parse_status=result.parse_status,
    )


def enrich_embedded_sql(item: EmbeddedSqlEntity) -> EmbeddedSqlEntity:
    result = parse_lineage(item.sql_text)
    item.referenced_tables = result.referenced_tables
    item.referenced_procs = result.referenced_procs
    item.parse_status = result.parse_status
    item.unresolved_reason = result.unresolved_reason
    return item
