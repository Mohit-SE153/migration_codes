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
    expression_dependencies = [("dbo", "trg_Audit", "SQL_TRIGGER", "dbo", "AuditLog", "OBJECT_OR_COLUMN")]
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
    expression_dependencies = [("dbo", "usp_Clean", "SQL_STORED_PROCEDURE", "dbo", "SomeTable", "OBJECT_OR_COLUMN")]
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
        ("dbo", "trg_Audit", "SQL_TRIGGER", None, "inserted", "OBJECT_OR_COLUMN"),
        ("dbo", "trg_Audit", "SQL_TRIGGER", "dbo", "RealTable", "OBJECT_OR_COLUMN"),
    ]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], triggers=[trigger],
        expression_dependencies=expression_dependencies,
    )
    targets = {d.target_object for d in deps if d.relationship_type != "fires_on"}
    assert targets == {"dbo.RealTable"}
    assert "inserted" not in targets


def test_backfill_source_type_not_recognized_is_skipped():
    expression_dependencies = [("dbo", "SomeTable", "USER_TABLE", "dbo", "ufnCalc", "OBJECT_OR_COLUMN")]
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], expression_dependencies=expression_dependencies)
    assert deps == []


def test_backfill_constraint_uses_three_part_source_object():
    constraint = ConstraintEntity(
        database="db", schema="dbo", table="Orders", name="CK_Orders_Total",
        constraint_type="CHECK", definition="([Total] > 0)",
        parse_status="unresolved", unresolved_reason="sqlglot parse error",
    )
    expression_dependencies = [("dbo", "CK_Orders_Total", "CHECK_CONSTRAINT", "dbo", "OtherTable", "OBJECT_OR_COLUMN")]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], constraints=[constraint],
        expression_dependencies=expression_dependencies,
    )
    backfilled = [d for d in deps if d.discovery_method == "direct_metadata" and d.source_type == "constraint"]
    assert len(backfilled) == 1
    assert backfilled[0].source_object == "dbo.Orders.CK_Orders_Total"


# --- Round 2: Table -> Sequence (companion edge for DEFAULT constraints) ---

def test_default_constraint_sequence_also_produces_a_table_level_edge():
    constraint = ConstraintEntity(
        database="db", schema="dbo", table="Orders", name="DF_Orders_Id",
        constraint_type="DEFAULT", definition="(NEXT VALUE FOR [dbo].[OrderIdSeq])",
    )
    enrich_constraint(constraint)
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], constraints=[constraint])
    seq_edges = [d for d in deps if d.target_type == "sequence"]
    sources = {(d.source_object, d.source_type) for d in seq_edges}
    assert ("dbo.Orders.DF_Orders_Id", "constraint") in sources
    assert ("dbo.Orders", "table") in sources
    assert len(seq_edges) == 2


def test_check_constraint_sequence_reference_has_no_table_companion_edge():
    """CHECK constraints can never contain NEXT VALUE FOR (SQL Server
    rejects it outright), so referenced_sequences will always be empty for
    them -- confirming the companion edge is scoped to constraint_type ==
    'DEFAULT' only, never fires for CHECK even if referenced_sequences were
    somehow non-empty."""
    constraint = ConstraintEntity(
        database="db", schema="dbo", table="Orders", name="CK_Orders_Total",
        constraint_type="CHECK", definition="([Total] > 0)", referenced_sequences=["dbo.SomeSeq"],
    )
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], constraints=[constraint])
    table_seq_edges = [d for d in deps if d.target_type == "sequence" and d.source_type == "table"]
    assert table_seq_edges == []


# --- Round 2: Procedure/Function -> UserDefinedType / XmlSchemaCollection (TYPE / XML_NAMESPACE classes) ---

def test_type_class_row_produces_unconditional_user_defined_type_edge():
    """Unlike OBJECT_OR_COLUMN backfill, TYPE-class edges apply even when
    the source object's own sqlglot parse was clean -- sqlglot has no way
    to see parameter/variable typing at all, gated or not."""
    proc = StoredProcedureEntity(
        database="db", schema="HumanResources", name="uspUpdateEmployeeHireInfo", loc=1,
        parse_status="sqlglot", unresolved_reason=None,
    )
    expression_dependencies = [("HumanResources", "uspUpdateEmployeeHireInfo", "SQL_STORED_PROCEDURE", "dbo", "Flag", "TYPE")]
    deps = build_dependency_graph(
        stored_procedures=[proc], views=[], packages=[], foreign_keys=[], expression_dependencies=expression_dependencies,
    )
    type_edges = [d for d in deps if d.target_type == "user_defined_type"]
    assert len(type_edges) == 1
    assert type_edges[0].source_object == "HumanResources.uspUpdateEmployeeHireInfo"
    assert type_edges[0].target_object == "dbo.Flag"
    assert type_edges[0].relationship_type == "uses_type"
    assert type_edges[0].discovery_method == "direct_metadata"


def test_xml_namespace_class_row_produces_xml_schema_collection_edge():
    func = FunctionEntity(
        database="db", schema="dbo", name="ufnParseDoc", function_type="SCALAR",
        parse_status="sqlglot", unresolved_reason=None,
    )
    expression_dependencies = [("dbo", "ufnParseDoc", "SQL_SCALAR_FUNCTION", "dbo", "MySchemaCollection", "XML_NAMESPACE")]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], functions=[func],
        expression_dependencies=expression_dependencies,
    )
    xml_edges = [d for d in deps if d.target_type == "xml_schema_collection"]
    assert len(xml_edges) == 1
    assert xml_edges[0].source_object == "dbo.ufnParseDoc"
    assert xml_edges[0].target_object == "dbo.MySchemaCollection"


def test_irrelevant_expression_dependency_class_is_ignored():
    """DATABASE/SCHEMA/ASSEMBLY-class rows are internal bookkeeping with no
    migration value -- confirms build_dependency_graph doesn't misroute an
    unrecognized class into either the OBJECT_OR_COLUMN or TYPE path."""
    proc = StoredProcedureEntity(database="db", schema="dbo", name="usp_a", loc=1, parse_status="sqlglot")
    expression_dependencies = [("dbo", "usp_a", "SQL_STORED_PROCEDURE", "dbo", "SomeSchema", "SCHEMA")]
    deps = build_dependency_graph(
        stored_procedures=[proc], views=[], packages=[], foreign_keys=[], expression_dependencies=expression_dependencies,
    )
    assert deps == []


# --- Round 2: Function -> UserDefinedType via scalar return type ---

def test_function_returning_a_known_udt_produces_uses_type_edge():
    functions = [FunctionEntity(database="db", schema="dbo", name="ufnGetPhone", function_type="SCALAR", return_type="Phone")]
    udts = [UserDefinedTypeEntity(database="db", schema="dbo", name="Phone", type_kind="ALIAS", base_type="nvarchar(25)")]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], functions=functions, user_defined_types=udts,
    )
    type_edges = [d for d in deps if d.source_type == "function" and d.target_type == "user_defined_type"]
    assert len(type_edges) == 1
    assert type_edges[0].source_object == "dbo.ufnGetPhone"
    assert type_edges[0].target_object == "dbo.Phone"


def test_function_returning_a_builtin_type_produces_no_edge():
    functions = [FunctionEntity(database="db", schema="dbo", name="ufnGetCount", function_type="SCALAR", return_type="int")]
    udts = [UserDefinedTypeEntity(database="db", schema="dbo", name="Phone", type_kind="ALIAS", base_type="nvarchar(25)")]
    deps = build_dependency_graph(
        stored_procedures=[], views=[], packages=[], foreign_keys=[], functions=functions, user_defined_types=udts,
    )
    assert not [d for d in deps if d.source_type == "function" and d.target_type == "user_defined_type"]
