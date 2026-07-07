"""
Targeted tests for lakebridge_discovery.dependency_extractor -- the
Lakebridge-native (no SQLGlot involved) regex gap-filler that adds any
dependency edge report_parser.py's extract_native_report_dependencies() (the
Analyzer-report-native path, tested separately in
test_lakebridge_report_parser_native_dependencies.py) didn't already find
(edge-level dedup, not whole-object skipping). Exercises it against a
tmp_path standing in for <source_export_dir>/sql/, with a small object
inventory built by hand (report_parser.py's own output shape).
"""
from __future__ import annotations

from pathlib import Path

from lakebridge_discovery.dependency_extractor import extract_dependencies
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult, LakebridgeObjectRef


def _write_sql(sql_dir: Path, filename: str, text: str) -> None:
    (sql_dir / filename).write_text(text, encoding="utf-8")


def _base_result() -> LakebridgeDiscoveryResult:
    result = LakebridgeDiscoveryResult()
    result.tables = [
        LakebridgeObjectRef(object_type="table", name="Sales.Store", source_tech="MS SQL Server"),
        LakebridgeObjectRef(object_type="table", name="Sales.Customer", source_tech="MS SQL Server"),
    ]
    result.views = [
        LakebridgeObjectRef(object_type="view", name="Sales.vStoreWithDemographics", source_tech="MS SQL Server"),
    ]
    result.stored_procedures = [
        LakebridgeObjectRef(object_type="stored_procedure", name="dbo.uspGetStoreInfo", source_tech="MS SQL Server"),
        LakebridgeObjectRef(object_type="stored_procedure", name="dbo.uspLogError", source_tech="MS SQL Server"),
    ]
    return result


def test_extract_dependencies_finds_reads_writes_calls(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()

    _write_sql(sql_dir, "view__Sales.vStoreWithDemographics.sql", """
        CREATE VIEW Sales.vStoreWithDemographics AS
        SELECT s.Name FROM Sales.Store s JOIN Sales.Customer c ON c.StoreID = s.BusinessEntityID
    """)
    _write_sql(sql_dir, "sql_stored_procedure__dbo.uspGetStoreInfo.sql", """
        CREATE PROCEDURE dbo.uspGetStoreInfo AS
        BEGIN
            INSERT INTO Sales.Store (Name) VALUES ('New Store');
            EXEC dbo.uspLogError;
        END
    """)

    result = _base_result()
    extract_dependencies(result, tmp_path)

    edges = {(d.source_object, d.target_object, d.relationship_type) for d in result.dependencies}
    assert ("Sales.vStoreWithDemographics", "sales.store", "reads") in edges
    assert ("Sales.vStoreWithDemographics", "sales.customer", "reads") in edges
    assert ("dbo.uspGetStoreInfo", "sales.store", "writes") in edges

    call_edges = [d for d in result.dependencies if d.source_object == "dbo.uspGetStoreInfo" and d.relationship_type == "calls"]
    assert len(call_edges) == 1
    assert call_edges[0].target_object == "dbo.usplogerror"
    assert call_edges[0].target_type == "stored_procedure"
    assert call_edges[0].resolved is True
    assert call_edges[0].discovery_method == "lakebridge"


def test_extract_dependencies_matches_bracket_quoted_identifiers(tmp_path):
    """Regression test: source_exporter.py's real exported SQL Server DDL
    overwhelmingly uses bracket-quoted identifiers ("FROM [Sales].[Store]"),
    not bare names -- found by running this against a real Analyzer export,
    where the regex patterns' leading character class rejected "[" and so
    silently matched nothing against actual files."""
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    _write_sql(sql_dir, "view__Sales.vStoreWithDemographics.sql", """
        CREATE VIEW [Sales].[vStoreWithDemographics] AS
        SELECT s.[Name] FROM [Sales].[Store] s
    """)

    result = _base_result()
    extract_dependencies(result, tmp_path)

    edges = {(d.source_object, d.target_object, d.relationship_type) for d in result.dependencies}
    assert ("Sales.vStoreWithDemographics", "sales.store", "reads") in edges


def test_extract_dependencies_marks_schema_qualified_unknown_as_unresolved(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    _write_sql(sql_dir, "view__Sales.vStoreWithDemographics.sql", """
        CREATE VIEW Sales.vStoreWithDemographics AS
        SELECT * FROM dbo.SomeTableNotInInventory
    """)

    result = _base_result()
    extract_dependencies(result, tmp_path)

    unresolved = [d for d in result.dependencies if not d.resolved]
    assert len(unresolved) == 1
    assert unresolved[0].target_object == "dbo.sometablenotininventory"
    assert unresolved[0].target_type == "unknown"


def test_extract_dependencies_skips_unqualified_unresolved_names(tmp_path):
    """An unqualified FROM target that matches nothing (typical of a CTE or
    derived-table alias) must not be emitted as a fabricated edge."""
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    _write_sql(sql_dir, "view__Sales.vStoreWithDemographics.sql", """
        WITH RecentOrders AS (SELECT 1 AS x)
        SELECT * FROM RecentOrders
    """)

    result = _base_result()
    extract_dependencies(result, tmp_path)

    assert result.dependencies == []


def test_extract_dependencies_populates_stats(tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    _write_sql(sql_dir, "view__Sales.vStoreWithDemographics.sql", """
        CREATE VIEW Sales.vStoreWithDemographics AS SELECT * FROM Sales.Store
    """)

    result = _base_result()
    extract_dependencies(result, tmp_path)

    stats = result.dependency_stats
    assert stats["total_dependencies"] == 1
    assert stats["by_relationship_type"] == {"reads": 1}
    assert stats["by_type_pair"] == {"view->table": 1}
    assert stats["resolved"] == 1
    assert stats["unresolved"] == 0


def test_extract_dependencies_no_export_dir_is_a_noop_with_warning(tmp_path):
    result = _base_result()
    extract_dependencies(result, tmp_path / "does_not_exist")

    assert result.dependencies == []
    assert result.dependency_stats["total_dependencies"] == 0
    assert any("not found" in w for w in result.warnings)


def test_extract_dependencies_only_adds_edges_not_already_known(tmp_path):
    """An edge report_parser.py's native extraction already found
    (discovery_method="lakebridge_report") must not be duplicated by the
    regex pass -- but a *different* edge from the same file that the native
    pass didn't cover (e.g. because object_lineage structurally can't
    represent it) must still be added. Gap-filling is edge-level, not
    whole-object-level -- see this module's docstring for why."""
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    _write_sql(sql_dir, "view__Sales.vStoreWithDemographics.sql", """
        CREATE VIEW Sales.vStoreWithDemographics AS
        SELECT * FROM Sales.Store JOIN Sales.Customer
    """)

    result = _base_result()
    result.dependencies.append(LakebridgeDependencyRef(
        source_object="Sales.vStoreWithDemographics", target_object="sales.store", relationship_type="reads",
        source_type="view", target_type="table", discovery_method="lakebridge_report", resolved=True,
    ))
    extract_dependencies(result, tmp_path)

    text_scan_edges = {(d.source_object, d.target_object) for d in result.dependencies if d.discovery_method == "lakebridge"}
    assert text_scan_edges == {("Sales.vStoreWithDemographics", "sales.customer")}  # only the genuinely new edge
    assert len(result.dependencies) == 2  # native Store edge (deduped, not doubled) + new Customer edge


def test_extract_dependencies_matches_bare_insert_without_into(tmp_path):
    """Regression test: T-SQL's INSERT statement makes INTO optional
    ("INSERT dbo.Foo (...)" is valid) -- found via a real proc (uspLogError)
    that uses the bare form, which used to never match."""
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    _write_sql(sql_dir, "sql_stored_procedure__dbo.uspGetStoreInfo.sql", """
        CREATE PROCEDURE dbo.uspGetStoreInfo AS
        BEGIN
            INSERT Sales.Store (Name) VALUES ('New Store');
        END
    """)

    result = _base_result()
    extract_dependencies(result, tmp_path)

    edges = {(d.source_object, d.target_object, d.relationship_type) for d in result.dependencies}
    assert ("dbo.uspGetStoreInfo", "sales.store", "writes") in edges


def test_extract_dependencies_detects_fires_on_for_triggers(tmp_path):
    """Regression test: CREATE TRIGGER's "ON <table>" clause is a structural
    fact (which table the trigger fires on) discoverable straight from the
    trigger's own header, distinct from reads/writes/calls."""
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    _write_sql(sql_dir, "sql_trigger__Sales.uSalesOrderHeader.sql", """
        CREATE TRIGGER [Sales].[uSalesOrderHeader] ON [Sales].[Store]
        AFTER UPDATE AS
        BEGIN
            SET NOCOUNT ON;
        END
    """)

    result = _base_result()
    result.triggers = [
        LakebridgeObjectRef(object_type="trigger", name="Sales.uSalesOrderHeader", source_tech="MS SQL Server"),
    ]
    extract_dependencies(result, tmp_path)

    edges = {(d.source_object, d.target_object, d.relationship_type) for d in result.dependencies}
    assert ("Sales.uSalesOrderHeader", "sales.store", "fires_on") in edges


def test_extract_dependencies_detects_inline_function_calls():
    """Regression test: a scalar function invoked inline
    ("SET @x = [dbo].[ufnGetAccountingStartDate]();") isn't an EXEC
    statement, so only a name-anchored pattern built from known function
    names in the inventory (never a generic "identifier(" match, to avoid
    false positives) can find it."""
    import tempfile
    from pathlib import Path as _Path

    result = _base_result()
    result.functions = [
        LakebridgeObjectRef(object_type="function", name="dbo.ufnGetAccountingStartDate", source_tech="MS SQL Server"),
    ]
    result.triggers = [
        LakebridgeObjectRef(object_type="trigger", name="Sales.uSalesOrderHeader", source_tech="MS SQL Server"),
    ]

    with tempfile.TemporaryDirectory() as td:
        tmp_path = _Path(td)
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        _write_sql(sql_dir, "sql_trigger__Sales.uSalesOrderHeader.sql", """
            CREATE TRIGGER [Sales].[uSalesOrderHeader] ON [Sales].[SalesOrderHeader]
            AFTER UPDATE AS
            BEGIN
                DECLARE @StartDate datetime;
                SET @StartDate = [dbo].[ufnGetAccountingStartDate]();
            END
        """)
        extract_dependencies(result, tmp_path)

    edges = {(d.source_object, d.target_object, d.relationship_type) for d in result.dependencies}
    assert ("Sales.uSalesOrderHeader", "dbo.ufngetaccountingstartdate", "calls") in edges


def test_extract_dependencies_function_call_pattern_ignores_own_create_header():
    """Regression test: a function's own "CREATE FUNCTION [dbo].[Name]("
    header matches the function-call pattern against its own name (the
    parameter list's opening paren) -- must not be emitted as "X calls X"."""
    import tempfile
    from pathlib import Path as _Path

    result = _base_result()
    result.functions = [
        LakebridgeObjectRef(object_type="function", name="dbo.ufnGetAccountingEndDate", source_tech="MS SQL Server"),
    ]
    with tempfile.TemporaryDirectory() as td:
        tmp_path = _Path(td)
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        _write_sql(sql_dir, "sql_scalar_function__dbo.ufnGetAccountingEndDate.sql", """
            CREATE FUNCTION [dbo].[ufnGetAccountingEndDate]()
            RETURNS [datetime]
            AS
            BEGIN
                RETURN CONVERT(datetime, '20030630', 101);
            END
        """)
        extract_dependencies(result, tmp_path)

    assert result.dependencies == []


def test_extract_dependencies_function_call_pattern_has_no_false_positives_on_builtins():
    """A generic "identifier(" scan would misfire on built-in functions like
    CAST/COUNT/GETDATE -- the name-anchored pattern must not, since it's
    built only from known function names in the inventory."""
    sql_dir_result = _base_result()
    sql_dir_result.functions = [
        LakebridgeObjectRef(object_type="function", name="dbo.ufnGetStock", source_tech="MS SQL Server"),
    ]
    import tempfile
    from pathlib import Path as _Path
    with tempfile.TemporaryDirectory() as td:
        tmp_path = _Path(td)
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        _write_sql(sql_dir, "view__Sales.vStoreWithDemographics.sql", """
            CREATE VIEW Sales.vStoreWithDemographics AS
            SELECT CAST(GETDATE() AS date), COUNT(*) FROM Sales.Store
        """)
        extract_dependencies(sql_dir_result, tmp_path)

    calls_edges = [d for d in sql_dir_result.dependencies if d.relationship_type == "calls"]
    assert calls_edges == []
