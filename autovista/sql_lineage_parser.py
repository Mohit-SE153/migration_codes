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
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from autovista.schema import (
    ConstraintEntity,
    EmbeddedSqlEntity,
    FunctionEntity,
    StoredProcedureEntity,
    TriggerEntity,
    ViewEntity,
)

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

# CREATE TRIGGER isn't in sqlglot's tsql grammar at all (confirmed
# empirically: unlike CREATE PROCEDURE/VIEW, the entire statement -- header
# and body alike -- degrades to one opaque Command node). The trigger body
# statements themselves parse fine once the declaration header and the one
# outermost BEGIN/END wrapper are stripped -- verified against real (and
# multi-event `AFTER INSERT, UPDATE`) trigger text. Only the OUTERMOST
# BEGIN/END is stripped (first BEGIN, last END) -- inner BEGIN/END (nested
# IF blocks) and CASE...END are left alone; a trigger body still too
# complex for sqlglot's tsql grammar after this correctly falls through to
# parse_lineage()'s existing unresolved/Command-node handling rather than
# guessing.
_TRIGGER_HEADER = re.compile(
    r"^\s*CREATE\s+TRIGGER\s+.*?\bON\b.*?\b(?:AFTER|INSTEAD\s+OF)\b.*?\bAS\b",
    re.IGNORECASE | re.DOTALL,
)
_FIRST_BEGIN = re.compile(r"\bBEGIN\b", re.IGNORECASE)
_LAST_END = re.compile(r"\bEND\b\s*;?\s*$", re.IGNORECASE)

# SQL Server's magic trigger-context virtual tables -- real T-SQL
# identifiers, but not database objects, so reporting them as dependency
# targets would be misleading (a downstream consumer would see "trigger
# depends on table 'inserted'", which doesn't exist).
_TRIGGER_PSEUDO_TABLES = {"inserted", "deleted"}


def _strip_trigger_declaration(definition: str) -> str:
    text = _TRIGGER_HEADER.sub("", definition, count=1)
    text = _FIRST_BEGIN.sub("", text, count=1)
    text = _LAST_END.sub("", text, count=1)
    return text.strip()


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
    r"\b(?:FROM|JOIN|INSERT(?:\s+INTO)?|UPDATE|DELETE(?:\s+FROM)?)\s+(?!@)\[?(\w+)\]?(?:\s*\.\s*\[?(\w+)\]?)?",
    re.IGNORECASE,
)

# Table-valued functions and other keywords that can follow FROM/JOIN but
# are not table names -- excluded so the regex fallback doesn't report
# e.g. "CONTAINSTABLE" as a referenced table for a full-text search call
# like `FROM CONTAINSTABLE(HumanResources.JobCandidate, *, @query)`.
#
# "INTO" specifically: found via live validation against a real
# AdventureWorks2022 function (ufnGetContactInformation, a multi-statement
# table-valued function -- `RETURNS @var TABLE (...)`, a construct
# sqlglot's tsql grammar doesn't parse at all). `INSERT INTO @tablevar` has
# no real identifier for `(\w+)` to match after "@" (the `(?!@)` guard
# above stops it matching the variable name itself), so the regex engine
# backtracks: it un-matches the optional "INTO", falls back to just
# "INSERT" as the keyword, and then captures the literal word "INTO" as if
# it were the table name. Excluding it here is the direct fix for that
# specific backtrack, on top of the `(?!@)` guard for the general case.
_NOT_A_TABLE = {
    "SELECT", "VALUES", "CONTAINSTABLE", "FREETEXTTABLE", "OPENROWSET",
    "OPENQUERY", "OPENXML", "OPENDATASOURCE", "OPENJSON", "INTO",
}


def _extract_table_like_refs(text: str) -> set[str]:
    refs = set()
    for first, second in _TABLE_REF_IN_COMMAND_TEXT.findall(text):
        if first.upper() in _NOT_A_TABLE:
            continue
        refs.add(f"{first}.{second}" if second else first)
    return refs


# Table variables (`DECLARE @x TABLE (...)`, or a multi-statement
# table-valued function's `RETURNS @x TABLE (...)`) -- found via live
# validation against a real AdventureWorks2022 function
# (ufnGetContactInformation): sqlglot's tsql AST parses
# `INSERT INTO @retContactInformation ...` as a genuine exp.Table node
# with name="retContactInformation" -- the "@" sigil is dropped entirely
# during parsing, with nothing else on the node to tell a table variable
# apart from a real unqualified table reference. Since the declaration
# always keeps its "@" in the raw source text, collecting declared
# variable names from there and filtering them out of the AST-derived
# table set is the only way to catch this.
_TABLE_VARIABLE_DECLARATION = re.compile(r"@(\w+)\s+TABLE\b", re.IGNORECASE)


def _collect_table_variable_names(sql_text: str) -> set[str]:
    return {m.lower() for m in _TABLE_VARIABLE_DECLARATION.findall(sql_text)}


@dataclass
class LineageResult:
    referenced_tables: list[str]
    referenced_procs: list[str]
    parse_status: str  # "sqlglot" | "unresolved"
    unresolved_reason: str | None = None
    # Populated only when known_function_names is passed to parse_lineage().
    referenced_functions: list[str] = field(default_factory=list)
    # `NEXT VALUE FOR schema.SequenceName` -- sqlglot's tsql grammar parses
    # this into a dedicated exp.NextValueFor node (verified empirically),
    # so detection needs no name cross-referencing (unlike function calls,
    # there's no exp.Anonymous ambiguity to resolve here).
    referenced_sequences: list[str] = field(default_factory=list)


def _normalize_table_name(table: exp.Table) -> str:
    """schema.table for the common case, extended to database.schema.table
    (3-part cross-database references, e.g. `OtherDB.dbo.SomeTable`) and
    best-effort server.database.schema.table (4-part linked-server
    references). sqlglot's exp.Table only has three named slots (catalog/
    db/name) even for tsql -- for a 3-part name these map directly to
    database/schema/table, but for a genuine 4-part name the schema and
    table both end up nested together inside `table.this` as a Dot
    expression (`this.this`=schema, `this.expression`=table) rather than
    in their own slots. Verified empirically against sqlglot (not assumed):
    `Server1.OtherDB.dbo.SomeTable` -> catalog=Server1, db=OtherDB,
    this=Dot(this=dbo, expression=SomeTable)."""
    if isinstance(table.this, exp.Dot) and table.catalog:
        schema_part = table.this.this.name if isinstance(table.this.this, exp.Identifier) else str(table.this.this)
        table_part = table.this.expression.name if isinstance(table.this.expression, exp.Identifier) else str(table.this.expression)
        parts = [p for p in (table.catalog, table.db, schema_part, table_part) if p]
        return ".".join(parts)
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts) if parts else table.name


def _extract_sequence_refs(body: exp.Expression) -> set[str]:
    """`NEXT VALUE FOR schema.SequenceName` parses into a dedicated
    exp.NextValueFor node whose `.this` is a Column (table=schema,
    this=sequence name) -- verified empirically against sqlglot's tsql
    dialect. Unqualified (`NEXT VALUE FOR MySeq`, no schema) references
    are reported by bare name; SQL Server itself defaults these to the
    caller's default schema, which isn't knowable from text alone, so no
    schema is guessed here."""
    refs: set[str] = set()
    for nv in body.find_all(exp.NextValueFor):
        col = nv.this
        if not isinstance(col, exp.Column) or not col.name:
            continue
        parts = [p for p in (col.table, col.name) if p]
        refs.add(".".join(parts))
    return refs


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


def _extract_function_calls(body: exp.Expression, known_function_names: frozenset[str]) -> set[str]:
    """Finds calls to user-defined functions -- both inline scalar calls
    (`SELECT dbo.ufnFoo(x) ...`) and table-valued function calls in FROM
    position (`FROM dbo.ufnBar(1)`).

    sqlglot parses a call to any name it doesn't recognize as a built-in
    into an `exp.Anonymous` node (confirmed empirically: `GETDATE()`/
    `CONVERT(...)` parse into their own dedicated exp subclasses, while
    `dbo.ufnGetOrderStatus(x)` parses as `exp.Anonymous(this="ufnGetOrderStatus")`
    with NO schema captured anywhere on the node -- the "dbo." prefix is
    dropped during parsing). Since the schema can't be recovered from the
    call site, matching is by bare function name only (case-insensitive)
    against `known_function_names` (schema.name pairs from this database's
    own Discovery-extracted function list) -- this is what resolves the
    schema, and also what keeps this from reporting a false positive for
    every unrecognized identifier sqlglot couldn't classify (a CLR type
    constructor, a system function sqlglot's grammar doesn't know about,
    etc.): only names that are confirmed real functions in this database
    are reported.

    A table-valued function call in FROM position parses as an exp.Table
    whose `.this` is the exp.Anonymous node (confirmed empirically:
    `FROM dbo.ufnBar(1)` -> Table(this=Anonymous(this="ufnBar"), db="dbo"),
    with Table.name == "" -- which is also why the plain table walk in
    parse_lineage() already skips it via the `if not table.name` check
    rather than misreporting it as a table)."""
    by_lower_name: dict[str, str] = {}
    for qualified in known_function_names:
        bare = qualified.rsplit(".", 1)[-1]
        by_lower_name[bare.lower()] = qualified

    found: set[str] = set()
    for anon in body.find_all(exp.Anonymous):
        # anon.this is a bare str for an unquoted call (`dbo.ufnFoo(x)`) but
        # an exp.Identifier for a bracket-quoted one (`[dbo].[ufnFoo](x)`,
        # the common case in real, SSMS-scripted SQL Server code) --
        # verified empirically against both forms.
        raw_name = anon.this.this if isinstance(anon.this, exp.Identifier) else anon.this
        name = (raw_name or "").lower()
        if name in by_lower_name:
            found.add(by_lower_name[name])
    return found


def parse_lineage(sql_text: str, dialect: str = "tsql", known_function_names: frozenset[str] | None = None) -> LineageResult:
    """known_function_names is optional and additive: omit it (the default
    for every pre-existing caller) and behavior is byte-identical to
    before this parameter existed -- referenced_functions just stays
    empty. Pass a set of "schema.name" function names (from this
    database's own Discovery-extracted function list) to also detect
    inline user-defined-function calls (see _extract_function_calls)."""
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
    functions: set[str] = set()
    sequences: set[str] = set()
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

        if known_function_names:
            functions.update(_extract_function_calls(body, known_function_names))

        sequences.update(_extract_sequence_refs(body))

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

    table_variable_names = _collect_table_variable_names(sql_text)
    tables = {t for t in tables if t.lower() not in table_variable_names}

    return LineageResult(
        referenced_tables=sorted(tables),
        referenced_procs=sorted(procs),
        parse_status="sqlglot",
        unresolved_reason=(
            "sqlglot fell back to a generic Command node for part of this statement -- "
            "referenced_tables/referenced_procs may be incomplete; verify manually"
        ) if saw_unsupported else None,
        referenced_functions=sorted(functions),
        referenced_sequences=sorted(sequences),
    )


def enrich_stored_procedure(
    proc: StoredProcedureEntity, definition: str, known_function_names: frozenset[str] | None = None,
) -> StoredProcedureEntity:
    """Fills in referenced_tables/referenced_procs on a StoredProcedureEntity
    whose existence/loc were already established via direct_metadata."""
    result = parse_lineage(definition, known_function_names=known_function_names)
    proc.referenced_tables = result.referenced_tables
    proc.referenced_procs = result.referenced_procs
    proc.referenced_functions = result.referenced_functions
    proc.referenced_sequences = result.referenced_sequences
    proc.parse_status = result.parse_status
    proc.unresolved_reason = result.unresolved_reason
    return proc


def build_view_entity(
    database: str, schema: str, name: str, definition: str, known_function_names: frozenset[str] | None = None,
) -> ViewEntity:
    result = parse_lineage(definition, known_function_names=known_function_names)
    return ViewEntity(
        database=database, schema=schema, name=name,
        referenced_tables=result.referenced_tables,
        referenced_functions=result.referenced_functions,
        referenced_sequences=result.referenced_sequences,
        parse_status=result.parse_status,
        unresolved_reason=result.unresolved_reason,
    )


def enrich_function(
    func: FunctionEntity, definition: str, known_function_names: frozenset[str] | None = None,
) -> FunctionEntity:
    """Fills in referenced_tables/referenced_functions on a FunctionEntity
    from its body text (`sys.sql_modules.definition`, fetched alongside the
    entity's own direct_metadata by sql_metadata_extractor.py). Functions
    can't EXEC a stored procedure (no side effects allowed in T-SQL
    functions), so there is no referenced_procs here -- Function -> Procedure
    isn't a real SQL Server dependency category."""
    result = parse_lineage(definition, known_function_names=known_function_names)
    func.referenced_tables = result.referenced_tables
    func.referenced_functions = result.referenced_functions
    func.referenced_sequences = result.referenced_sequences
    func.parse_status = result.parse_status
    func.unresolved_reason = result.unresolved_reason
    return func


def enrich_trigger(
    trigger: TriggerEntity, definition: str, known_function_names: frozenset[str] | None = None,
) -> TriggerEntity:
    """Fills in referenced_tables/referenced_procs/referenced_functions on a
    TriggerEntity from its body text. Triggers can both read other tables
    (e.g. a trigger on OrderDetails joining back to Orders) and EXEC a
    stored procedure, so this uses the full parse_lineage() feature set --
    unlike enrich_function, which omits referenced_procs.

    CREATE TRIGGER's declaration header/BEGIN-END wrapper isn't parseable
    by sqlglot's tsql grammar (see _strip_trigger_declaration) -- stripped
    before handing the body to parse_lineage(). "inserted"/"deleted"
    (SQL Server's magic trigger-context tables, not real objects) are
    filtered out of the result rather than reported as dependency targets."""
    stripped = _strip_trigger_declaration(definition)
    result = parse_lineage(stripped, known_function_names=known_function_names)
    trigger.referenced_tables = [
        t for t in result.referenced_tables if t.lower() not in _TRIGGER_PSEUDO_TABLES
    ]
    trigger.referenced_procs = result.referenced_procs
    trigger.referenced_functions = result.referenced_functions
    trigger.referenced_sequences = result.referenced_sequences
    trigger.parse_status = result.parse_status
    trigger.unresolved_reason = result.unresolved_reason
    return trigger


def enrich_constraint(
    constraint: ConstraintEntity, known_function_names: frozenset[str] | None = None,
) -> ConstraintEntity:
    """Fills in referenced_tables/referenced_functions on a CHECK/DEFAULT
    ConstraintEntity by parsing its already-captured `definition` text
    (Enhancement 2 already fetches this from sys.check_constraints /
    sys.default_constraints -- this just runs it through the same parser
    as everything else). No-op (returns unchanged) for PRIMARY_KEY/UNIQUE/
    FOREIGN_KEY constraints, which have no `definition` text to parse --
    foreign keys keep using the existing direct_metadata path unchanged."""
    if not constraint.definition:
        return constraint
    result = parse_lineage(constraint.definition, known_function_names=known_function_names)
    constraint.referenced_tables = result.referenced_tables
    constraint.referenced_functions = result.referenced_functions
    constraint.referenced_sequences = result.referenced_sequences
    constraint.unresolved_reason = result.unresolved_reason
    if result.parse_status == "sqlglot":
        constraint.parse_status = "sqlglot"
    return constraint


def enrich_embedded_sql(item: EmbeddedSqlEntity) -> EmbeddedSqlEntity:
    result = parse_lineage(item.sql_text)
    item.referenced_tables = result.referenced_tables
    item.referenced_procs = result.referenced_procs
    item.referenced_sequences = result.referenced_sequences
    item.parse_status = result.parse_status
    item.unresolved_reason = result.unresolved_reason
    return item
