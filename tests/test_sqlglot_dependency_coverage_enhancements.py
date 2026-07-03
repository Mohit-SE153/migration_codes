"""
Tests for the SQLGlot dependency-discovery coverage enhancements: sequence
references (NEXT VALUE FOR), resolve_target_type's proc/function/synonym
awareness, Table -> UserDefinedType/XmlSchemaCollection edges, computed
column -> function detection, and the sys.sql_expression_dependencies
metadata backfill. Existing dependency-graph behavior (FK/fires_on/synonym/
sqlglot-derived edges) is verified unaffected.
"""
from __future__ import annotations

from autovista.dependency_graph_builder import build_dependency_graph
from autovista.schema import (
    ColumnEntity,
    ConstraintEntity,
    FunctionEntity,
    StoredProcedureEntity,
    SynonymEntity,
    TableEntity,
    TriggerEntity,
    UserDefinedTypeEntity,
    ViewEntity,
)
from autovista.sql_lineage_parser import enrich_constraint, parse_lineage


def _table(schema: str, name: str, columns: list[ColumnEntity]) -> TableEntity:
    return TableEntity(
        database="db", schema=schema, name=name,
        row_count=0, size_mb=0.0, column_count=len(columns), columns=columns,
    )


def _column(name: str, data_type: str, **kwargs) -> ColumnEntity:
    return ColumnEntity(name=name, data_type=data_type, nullable=True, ordinal_position=1, **kwargs)


# --- NEXT VALUE FOR sequence detection ---

def test_next_value_for_is_detected_and_normalized():
    result = parse_lineage("SELECT NEXT VALUE FOR dbo.MySeq")
    assert result.referenced_sequences == ["dbo.MySeq"]


def test_next_value_for_in_default_constraint_definition():
    constraint = ConstraintEntity(
        database="db", schema="dbo", table="Orders", name="DF_Orders_Id",
        constraint_type="DEFAULT", definition="(NEXT VALUE FOR [dbo].[OrderIdSeq])",
    )
    enrich_constraint(constraint)
    assert constraint.referenced_sequences == ["dbo.OrderIdSeq"]


def test_no_sequence_reference_yields_empty_list():
    result = parse_lineage("SELECT * FROM dbo.Orders")
    assert result.referenced_sequences == []


def test_sequence_edge_generated_in_dependency_graph():
    proc = StoredProcedureEntity(
        database="db", schema="dbo", name="usp_a", loc=1,
        referenced_sequences=["dbo.OrderIdSeq"], parse_status="sqlglot",
    )
    deps = build_dependency_graph(stored_procedures=[proc], views=[], packages=[], foreign_keys=[])
    seq_edges = [d for d in deps if d.target_type == "sequence"]
    assert len(seq_edges) == 1
    assert seq_edges[0].source_object == "dbo.usp_a"
    assert seq_edges[0].target_object == "dbo.OrderIdSeq"
    assert seq_edges[0].relationship_type == "uses_sequence"


# --- resolve_target_type: proc/function/synonym awareness ---

def test_synonym_pointing_at_a_stored_procedure_is_classified_correctly():
    synonyms = [SynonymEntity(database="db", schema="dbo", name="RunReportAlias", base_object="dbo.usp_RunReport")]
    procs = [StoredProcedureEntity(database="db", schema="dbo", name="usp_RunReport", loc=1)]
    deps = build_dependency_graph(stored_procedures=procs, views=[], packages=[], foreign_keys=[], synonyms=synonyms)
    synonym_edge = next(d for d in deps if d.source_type == "synonym")
    assert synonym_edge.target_type == "stored_procedure"


def test_synonym_pointing_at_a_function_is_classified_correctly():
    synonyms = [SynonymEntity(database="db", schema="dbo", name="CalcAlias", base_object="dbo.ufnCalc")]
    functions = [FunctionEntity(database="db", schema="dbo", name="ufnCalc", function_type="SCALAR")]
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], functions=functions, synonyms=synonyms)
    synonym_edge = next(d for d in deps if d.source_type == "synonym")
    assert synonym_edge.target_type == "function"


def test_unrecognized_synonym_target_still_defaults_to_table():
    synonyms = [SynonymEntity(database="db", schema="dbo", name="OrdersAlias", base_object="dbo.Orders")]
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], synonyms=synonyms)
    synonym_edge = next(d for d in deps if d.source_type == "synonym")
    assert synonym_edge.target_type == "table"


# --- Table -> UserDefinedType ---

def test_table_column_using_a_known_udt_produces_uses_type_edge():
    tables = [_table("dbo", "Employee", [_column("SalariedFlag", "Flag")])]
    udts = [UserDefinedTypeEntity(database="db", schema="dbo", name="Flag", type_kind="ALIAS", base_type="bit")]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], tables=tables, user_defined_types=udts,
    )
    udt_edges = [d for d in deps if d.target_type == "user_defined_type"]
    assert len(udt_edges) == 1
    assert udt_edges[0].source_object == "dbo.Employee"
    assert udt_edges[0].target_object == "dbo.Flag"
    assert udt_edges[0].relationship_type == "uses_type"
    assert udt_edges[0].discovery_method == "direct_metadata"


def test_table_column_using_a_builtin_type_produces_no_udt_edge():
    tables = [_table("dbo", "Orders", [_column("OrderId", "int")])]
    udts = [UserDefinedTypeEntity(database="db", schema="dbo", name="Flag", type_kind="ALIAS", base_type="bit")]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], tables=tables, user_defined_types=udts,
    )
    assert not [d for d in deps if d.target_type == "user_defined_type"]


def test_two_columns_of_the_same_udt_collapse_to_one_edge():
    tables = [_table("dbo", "Person", [_column("FirstName", "Name"), _column("LastName", "Name")])]
    udts = [UserDefinedTypeEntity(database="db", schema="dbo", name="Name", type_kind="ALIAS", base_type="nvarchar(50)")]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], tables=tables, user_defined_types=udts,
    )
    assert len([d for d in deps if d.target_type == "user_defined_type"]) == 1


# --- Table -> XmlSchemaCollection ---

def test_xml_schema_collection_bound_column_produces_uses_type_edge():
    tables = [_table("Production", "ProductModel", [
        _column("CatalogDescription", "xml", xml_schema_collection="Production.ProductDescriptionSchemaCollection"),
    ])]
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], tables=tables)
    xml_edges = [d for d in deps if d.target_type == "xml_schema_collection"]
    assert len(xml_edges) == 1
    assert xml_edges[0].source_object == "Production.ProductModel"
    assert xml_edges[0].target_object == "Production.ProductDescriptionSchemaCollection"


def test_unbound_xml_column_produces_no_edge():
    tables = [_table("dbo", "Foo", [_column("Bar", "xml")])]
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], tables=tables)
    assert not [d for d in deps if d.target_type == "xml_schema_collection"]


# --- Computed column -> Function ---

def test_computed_column_function_reference_produces_calls_edge():
    tables = [_table("Sales", "Customer", [
        _column("AccountNumber", "nvarchar", computed_expression="(isnull('AW'+[dbo].[ufnLeadingZeros]([CustomerID]),''))",
                referenced_functions=["dbo.ufnLeadingZeros"]),
    ])]
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], tables=tables)
    func_edges = [d for d in deps if d.source_type == "table" and d.target_type == "function"]
    assert len(func_edges) == 1
    assert func_edges[0].source_object == "Sales.Customer"
    assert func_edges[0].target_object == "dbo.ufnLeadingZeros"
    assert func_edges[0].relationship_type == "calls"
    assert func_edges[0].discovery_method == "sqlglot"


def test_computed_column_without_function_call_produces_no_edge():
    tables = [_table("dbo", "Orders", [
        _column("LineTotal", "money", computed_expression="([UnitPrice]*[Qty])"),
    ])]
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], tables=tables)
    assert not [d for d in deps if d.source_type == "table" and d.target_type == "function"]


# --- Metadata backfill (sys.sql_expression_dependencies) ---

def test_backfill_fills_gap_for_a_trigger_whose_parse_failed():
    trigger = TriggerEntity(
        database="db", schema="dbo", name="trg_Audit", table="dbo.Orders", event="AFTER_UPDATE",
        parse_status="unresolved", unresolved_reason="sqlglot parse error: could not parse",
    )
    expression_dependencies = [("dbo", "trg_Audit", "SQL_TRIGGER", "dbo", "AuditLog")]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], triggers=[trigger],
        expression_dependencies=expression_dependencies,
    )
    backfilled = [d for d in deps if d.discovery_method == "direct_metadata" and d.relationship_type != "fires_on"]
    assert len(backfilled) == 1
    assert backfilled[0].target_object == "dbo.AuditLog"
    assert backfilled[0].relationship_type == "reads"


def test_backfill_does_not_apply_to_a_cleanly_parsed_object():
    proc = StoredProcedureEntity(
        database="db", schema="dbo", name="usp_Clean", loc=1, parse_status="sqlglot", unresolved_reason=None,
    )
    expression_dependencies = [("dbo", "usp_Clean", "SQL_STORED_PROCEDURE", "dbo", "SomeTable")]
    deps = build_dependency_graph(
        stored_procedures=[proc], views=[], packages=[], foreign_keys=[],
        expression_dependencies=expression_dependencies,
    )
    assert deps == []


def test_backfill_excludes_ambiguous_and_pseudo_table_rows_already_filtered_by_the_source():
    """Ambiguous/pseudo-table filtering happens in the MetadataSource
    implementation (see sql_metadata_extractor.py); this confirms
    build_dependency_graph still behaves correctly if a pseudo-table name
    somehow reaches it (defense in depth, not double-filtering)."""
    trigger = TriggerEntity(
        database="db", schema="dbo", name="trg_Audit", table="dbo.Orders", event="AFTER_INSERT",
        parse_status="unresolved", unresolved_reason="parse error",
    )
    expression_dependencies = [
        ("dbo", "trg_Audit", "SQL_TRIGGER", None, "inserted"),
        ("dbo", "trg_Audit", "SQL_TRIGGER", "dbo", "RealTable"),
    ]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], triggers=[trigger],
        expression_dependencies=expression_dependencies,
    )
    targets = {d.target_object for d in deps if d.relationship_type != "fires_on"}
    assert targets == {"dbo.RealTable"}
    assert "inserted" not in targets


def test_backfill_source_type_not_recognized_is_skipped():
    expression_dependencies = [("dbo", "SomeTable", "USER_TABLE", "dbo", "ufnCalc")]
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], expression_dependencies=expression_dependencies)
    assert deps == []


def test_backfill_constraint_uses_three_part_source_object():
    constraint = ConstraintEntity(
        database="db", schema="dbo", table="Orders", name="CK_Orders_Total",
        constraint_type="CHECK", definition="([Total] > 0)",
        parse_status="unresolved", unresolved_reason="sqlglot parse error",
    )
    expression_dependencies = [("dbo", "CK_Orders_Total", "CHECK_CONSTRAINT", "dbo", "OtherTable")]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], constraints=[constraint],
        expression_dependencies=expression_dependencies,
    )
    backfilled = [d for d in deps if d.discovery_method == "direct_metadata" and d.source_type == "constraint"]
    assert len(backfilled) == 1
    assert backfilled[0].source_object == "dbo.Orders.CK_Orders_Total"
