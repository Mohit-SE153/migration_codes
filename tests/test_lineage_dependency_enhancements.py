"""
Tests for the SQLGlot dependency-discovery enhancement: cross-database
name normalization, inline user-defined-function call detection, and the
new enrich_function/enrich_trigger/enrich_constraint entry points in
sql_lineage_parser.py. Existing parse_lineage/enrich_stored_procedure/
build_view_entity behavior is verified unaffected when the new optional
known_function_names parameter is omitted.
"""
from __future__ import annotations

from autovista.schema import ConstraintEntity, FunctionEntity, StoredProcedureEntity, TriggerEntity
from autovista.sql_lineage_parser import (
    enrich_constraint,
    enrich_function,
    enrich_stored_procedure,
    enrich_trigger,
    parse_lineage,
)


def test_existing_behavior_unaffected_when_known_function_names_omitted():
    result = parse_lineage("SELECT * FROM dbo.Orders")
    assert result.referenced_tables == ["dbo.Orders"]
    assert result.referenced_functions == []


def test_cross_database_three_part_name_is_fully_qualified():
    result = parse_lineage("SELECT * FROM OtherDB.dbo.SomeTable")
    assert result.referenced_tables == ["OtherDB.dbo.SomeTable"]


def test_four_part_linked_server_name_is_fully_qualified():
    result = parse_lineage("SELECT * FROM Server1.OtherDB.dbo.SomeTable")
    assert result.referenced_tables == ["Server1.OtherDB.dbo.SomeTable"]


def test_inline_scalar_function_call_is_detected_when_known():
    result = parse_lineage(
        "SELECT dbo.ufnGetOrderStatus(OrderId) FROM dbo.Orders",
        known_function_names=frozenset({"dbo.ufnGetOrderStatus"}),
    )
    assert result.referenced_tables == ["dbo.Orders"]
    assert result.referenced_functions == ["dbo.ufnGetOrderStatus"]


def test_unknown_function_call_is_not_reported_as_a_false_positive():
    """An unrecognized Anonymous-node call that ISN'T in the known function
    list must not be reported -- only confirmed real functions are."""
    result = parse_lineage(
        "SELECT dbo.SomeUnknownThing(OrderId) FROM dbo.Orders",
        known_function_names=frozenset({"dbo.ufnGetOrderStatus"}),
    )
    assert result.referenced_functions == []


def test_table_valued_function_call_in_from_clause_is_detected():
    """A TVF call in FROM position parses as an exp.Table with an empty
    name -- must not be misreported as a table, and must be recognized as
    a function call when known."""
    result = parse_lineage(
        "SELECT * FROM dbo.ufnGetContactInformation(1)",
        known_function_names=frozenset({"dbo.ufnGetContactInformation"}),
    )
    assert result.referenced_tables == []
    assert result.referenced_functions == ["dbo.ufnGetContactInformation"]


def test_builtin_functions_are_never_misreported_as_user_functions():
    result = parse_lineage(
        "SELECT GETDATE(), CONVERT(varchar(10), OrderId) FROM dbo.Orders",
        known_function_names=frozenset({"dbo.getdate", "dbo.convert"}),
    )
    assert result.referenced_functions == []


def test_enrich_stored_procedure_still_works_without_known_function_names():
    proc = StoredProcedureEntity(database="x", schema="dbo", name="usp_a", loc=1)
    enrich_stored_procedure(proc, "CREATE PROCEDURE dbo.usp_a AS SELECT * FROM dbo.Orders")
    assert proc.referenced_tables == ["dbo.Orders"]
    assert proc.referenced_functions == []


def test_enrich_stored_procedure_detects_function_calls():
    proc = StoredProcedureEntity(database="x", schema="dbo", name="usp_a", loc=1)
    enrich_stored_procedure(
        proc, "CREATE PROCEDURE dbo.usp_a AS SELECT dbo.ufnFoo(OrderId) FROM dbo.Orders",
        known_function_names=frozenset({"dbo.ufnFoo"}),
    )
    assert proc.referenced_tables == ["dbo.Orders"]
    assert proc.referenced_functions == ["dbo.ufnFoo"]


def test_build_view_entity_detects_function_calls():
    from autovista.sql_lineage_parser import build_view_entity
    view = build_view_entity(
        "x", "dbo", "v1", "CREATE VIEW dbo.v1 AS SELECT dbo.ufnFoo(OrderId) FROM dbo.Orders",
        known_function_names=frozenset({"dbo.ufnFoo"}),
    )
    assert view.referenced_tables == ["dbo.Orders"]
    assert view.referenced_functions == ["dbo.ufnFoo"]


def test_table_variable_is_not_reported_as_a_real_table():
    """`INSERT INTO @tablevar` -- sqlglot's AST strips the '@' sigil and
    represents this as a genuine exp.Table node named "tablevar", making it
    indistinguishable from a real unqualified table reference at the AST
    level. Found via live validation against a real AdventureWorks2022
    multi-statement table-valued function (ufnGetContactInformation)."""
    definition = (
        "CREATE FUNCTION dbo.ufnFoo() RETURNS @retInfo TABLE (PersonID int) AS BEGIN "
        "INSERT INTO @retInfo SELECT BusinessEntityID FROM Person.Person "
        "RETURN END"
    )
    result = parse_lineage(definition)
    assert "retInfo" not in result.referenced_tables
    assert "INTO" not in result.referenced_tables
    assert "Person.Person" in result.referenced_tables


def test_enrich_function_populates_referenced_tables_and_functions():
    func = FunctionEntity(database="x", schema="dbo", name="ufnA", function_type="SCALAR")
    enrich_function(
        func, "CREATE FUNCTION dbo.ufnA() RETURNS INT AS BEGIN RETURN (SELECT dbo.ufnB() FROM dbo.Orders) END",
        known_function_names=frozenset({"dbo.ufnA", "dbo.ufnB"}),
    )
    assert func.referenced_tables == ["dbo.Orders"]
    assert func.referenced_functions == ["dbo.ufnB"]
    assert func.parse_status == "sqlglot"


def test_enrich_trigger_strips_declaration_header_and_finds_body_references():
    trigger = TriggerEntity(database="x", schema="dbo", name="trg1", table="dbo.Orders", event="UPDATE")
    enrich_trigger(
        trigger,
        "CREATE TRIGGER dbo.trg1 ON dbo.Orders AFTER UPDATE AS BEGIN "
        "INSERT INTO dbo.AuditLog SELECT * FROM inserted; EXEC dbo.usp_Audit END",
    )
    assert trigger.referenced_tables == ["dbo.AuditLog"]
    assert trigger.referenced_procs == ["dbo.usp_Audit"]
    assert trigger.parse_status == "sqlglot"


def test_enrich_trigger_filters_inserted_and_deleted_pseudo_tables():
    trigger = TriggerEntity(database="x", schema="dbo", name="trg1", table="dbo.Orders", event="UPDATE")
    enrich_trigger(trigger, "CREATE TRIGGER dbo.trg1 ON dbo.Orders AFTER UPDATE AS BEGIN SELECT * FROM inserted END")
    assert "inserted" not in [t.lower() for t in trigger.referenced_tables]


def test_enrich_trigger_handles_multi_event_declaration():
    trigger = TriggerEntity(database="x", schema="dbo", name="trg2", table="dbo.Orders", event="INSERT,UPDATE")
    enrich_trigger(
        trigger,
        "CREATE TRIGGER dbo.trg2 ON dbo.Orders AFTER INSERT, UPDATE AS BEGIN "
        "UPDATE dbo.Inventory SET Qty = Qty - 1 FROM inserted i JOIN dbo.Inventory ON dbo.Inventory.ProductId = i.ProductId END",
    )
    assert trigger.referenced_tables == ["dbo.Inventory"]


def test_enrich_trigger_detects_function_calls():
    trigger = TriggerEntity(database="x", schema="dbo", name="trg3", table="dbo.Orders", event="UPDATE")
    enrich_trigger(
        trigger,
        "CREATE TRIGGER dbo.trg3 ON dbo.Orders AFTER UPDATE AS BEGIN "
        "INSERT INTO dbo.AuditLog SELECT dbo.ufnNow() END",
        known_function_names=frozenset({"dbo.ufnNow"}),
    )
    assert trigger.referenced_functions == ["dbo.ufnNow"]


def test_enrich_constraint_parses_check_constraint_calling_a_function():
    constraint = ConstraintEntity(
        database="x", schema="dbo", table="Orders", name="CK_x", constraint_type="CHECK",
        definition="(dbo.ufnIsValid([TotalDue]))",
    )
    enrich_constraint(constraint, known_function_names=frozenset({"dbo.ufnIsValid"}))
    assert constraint.referenced_functions == ["dbo.ufnIsValid"]
    assert constraint.parse_status == "sqlglot"


def test_enrich_constraint_parses_default_constraint_calling_a_function():
    constraint = ConstraintEntity(
        database="x", schema="dbo", table="Orders", name="DF_x", constraint_type="DEFAULT",
        definition="(dbo.ufnGetDefault())",
    )
    enrich_constraint(constraint, known_function_names=frozenset({"dbo.ufnGetDefault"}))
    assert constraint.referenced_functions == ["dbo.ufnGetDefault"]


def test_enrich_constraint_is_a_noop_for_constraints_without_definition_text():
    """PRIMARY_KEY/UNIQUE/FOREIGN_KEY constraints have no definition to
    parse -- must not raise or alter parse_status."""
    constraint = ConstraintEntity(
        database="x", schema="dbo", table="Orders", name="PK_Orders", constraint_type="PRIMARY_KEY",
        columns=["OrderId"],
    )
    enrich_constraint(constraint, known_function_names=frozenset({"dbo.ufnGetDefault"}))
    assert constraint.referenced_functions == []
    assert constraint.parse_status == "direct_metadata"
