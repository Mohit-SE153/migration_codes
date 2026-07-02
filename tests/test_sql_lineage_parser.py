from autovista.sql_lineage_parser import parse_lineage


def test_resolves_simple_select():
    r = parse_lineage("SELECT * FROM dbo.Customers")
    assert r.parse_status == "sqlglot"
    assert r.referenced_tables == ["dbo.Customers"]


def test_resolves_join():
    r = parse_lineage("SELECT o.OrderId FROM dbo.Orders o JOIN dbo.Customers c ON c.CustomerId = o.CustomerId")
    assert set(r.referenced_tables) == {"dbo.Orders", "dbo.Customers"}


def test_resolves_exec_as_proc_not_table():
    r = parse_lineage("EXEC dbo.usp_DoThing;")
    assert r.referenced_tables == []
    assert r.referenced_procs == ["dbo.usp_DoThing"]


def test_flags_dynamic_sql_as_unresolved():
    r = parse_lineage("EXEC sp_executesql @sql")
    assert r.parse_status == "unresolved"
    assert r.referenced_tables == [] and r.referenced_procs == []


def test_does_not_treat_update_target_alias_as_table():
    sql = """
    UPDATE i SET i.Qty = s.Qty
    FROM dbo.Inventory i INNER JOIN staging.stg_Inventory s ON s.ProductId = i.ProductId
    """
    r = parse_lineage(sql)
    assert set(r.referenced_tables) == {"dbo.Inventory", "staging.stg_Inventory"}


def test_garbage_input_is_unresolved_not_raised():
    r = parse_lineage("this is not %%% valid t-sql at !! all (((")
    assert r.parse_status == "unresolved"


# --- Regression tests for bugs found via live validation against a real
# AdventureWorks2022 instance (see spike/step0_report.md "Live validation
# addendum") ---

def test_option_maxrecursion_hint_does_not_break_recursive_cte_parsing():
    # sqlglot's tsql grammar has no support for the OPTION(...) query hint
    # clause; without stripping it, this whole statement used to fail and
    # report zero tables even though the CTE clearly references two real
    # tables (found on uspGetBillOfMaterials/uspGetWhereUsedProductID).
    sql = """
    WITH cte(ComponentID) AS (
        SELECT b.ComponentID FROM dbo.BillOfMaterials b WHERE b.ProductAssemblyID = 1
        UNION ALL
        SELECT b.ComponentID FROM cte INNER JOIN dbo.BillOfMaterials b ON b.ProductAssemblyID = cte.ComponentID
    )
    SELECT * FROM cte
    OPTION (MAXRECURSION 25)
    """
    r = parse_lineage(sql)
    assert r.parse_status == "sqlglot"
    assert r.referenced_tables == ["dbo.BillOfMaterials"]


def test_recursive_cte_self_reference_is_not_reported_as_a_table():
    # `FROM cte ...` inside the recursive member of a CTE (aliased or not)
    # must not show up in referenced_tables -- "cte" is not a real table.
    sql = """
    WITH cte(ComponentID) AS (
        SELECT b.ComponentID FROM dbo.BillOfMaterials b
        UNION ALL
        SELECT b.ComponentID FROM cte AS x INNER JOIN dbo.BillOfMaterials b ON b.ProductAssemblyID = x.ComponentID
    )
    SELECT * FROM cte
    """
    r = parse_lineage(sql)
    assert r.referenced_tables == ["dbo.BillOfMaterials"]
    assert "cte" not in r.referenced_tables


def test_try_catch_does_not_silently_drop_table_references():
    # sqlglot's tsql grammar has no concept of BEGIN TRY/BEGIN CATCH at
    # all; without flattening it out first, everything from the first
    # BEGIN TRY onward used to degrade into opaque, unsearched Command
    # nodes -- a *worse* failure mode than "unresolved" because it
    # reported parse_status="sqlglot" with a confidently empty (wrong)
    # table list instead of flagging anything.
    sql = """
    CREATE PROCEDURE dbo.usp_Test
    AS
    BEGIN
        BEGIN TRY
            UPDATE dbo.Foo SET x = 1 WHERE y = 2;
        END TRY
        BEGIN CATCH
            INSERT INTO dbo.ErrorLog (msg) VALUES (ERROR_MESSAGE());
        END CATCH
    END;
    """
    r = parse_lineage(sql)
    assert set(r.referenced_tables) >= {"dbo.Foo", "dbo.ErrorLog"}


def test_unparsed_command_fallback_is_flagged_for_review_not_silent():
    # A construct too deep for sqlglot's grammar (nested IF/BEGIN/END
    # inside a TRY block, as in AdventureWorks2022's real uspLogError)
    # must still surface *some* signal that the result may be incomplete,
    # rather than reporting parse_status="sqlglot" with no caveat.
    sql = """
    CREATE PROCEDURE dbo.usp_Test
    AS
    BEGIN
        BEGIN TRY
            IF 1 = 1
            BEGIN
                PRINT 'nope';
                RETURN;
            END
            INSERT INTO dbo.ErrorLog (msg) VALUES ('x');
        END TRY
        BEGIN CATCH
            RETURN -1;
        END CATCH
    END;
    """
    r = parse_lineage(sql)
    assert r.unresolved_reason is not None
    assert "dbo.ErrorLog" in r.referenced_tables


def test_fulltext_search_functions_are_not_reported_as_tables():
    # CONTAINSTABLE/FREETEXTTABLE are table-valued functions, not tables
    # -- the regex fallback for opaque Command-node text must not report
    # the function name itself as a referenced table.
    sql = """
    CREATE PROCEDURE dbo.usp_Test
    AS
    BEGIN
        BEGIN TRY
            IF 1 = 1
            BEGIN
                PRINT 'x';
            END
            SELECT jc.* FROM dbo.JobCandidate jc
            INNER JOIN CONTAINSTABLE(dbo.JobCandidate, *, 'engineer') AS ct ON jc.JobCandidateID = ct.[KEY];
        END TRY
        BEGIN CATCH
            RETURN -1;
        END CATCH
    END;
    """
    r = parse_lineage(sql)
    assert "CONTAINSTABLE" not in r.referenced_tables
    assert "dbo.JobCandidate" in r.referenced_tables
