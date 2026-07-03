"""
Tests for the extended dependency_graph_builder.py: new edge types
(function/trigger/constraint/synonym), table-vs-view target_type
classification, and deduplication. Existing proc/view/foreign_key/package
edge behavior is verified unchanged.
"""
from __future__ import annotations

from autovista.dependency_graph_builder import build_dependency_graph
from autovista.schema import (
    ConstraintEntity,
    FunctionEntity,
    StoredProcedureEntity,
    SynonymEntity,
    TriggerEntity,
    ViewEntity,
)


def test_existing_proc_view_foreign_key_edges_unchanged():
    proc = StoredProcedureEntity(
        database="x", schema="dbo", name="usp_a", loc=1,
        referenced_tables=["dbo.Orders"], referenced_procs=["dbo.usp_b"], parse_status="sqlglot",
    )
    view = ViewEntity(database="x", schema="dbo", name="v1", referenced_tables=["dbo.Orders"], parse_status="sqlglot")

    deps = build_dependency_graph(
        stored_procedures=[proc], views=[view], packages=[], foreign_keys=[("dbo.OrderDetails", "dbo.Orders")],
    )
    by_relationship = {(d.source_object, d.relationship_type, d.target_object) for d in deps}
    assert ("dbo.usp_a", "reads", "dbo.Orders") in by_relationship
    assert ("dbo.usp_a", "calls", "dbo.usp_b") in by_relationship
    assert ("dbo.v1", "reads", "dbo.Orders") in by_relationship
    assert ("dbo.OrderDetails", "foreign_key", "dbo.Orders") in by_relationship


def test_view_target_is_classified_as_view_not_table():
    """A proc reading from a name that matches a known view should get
    target_type='view', not the pre-existing blanket 'table'."""
    proc = StoredProcedureEntity(
        database="x", schema="dbo", name="usp_a", loc=1, referenced_tables=["dbo.v1"], parse_status="sqlglot",
    )
    view = ViewEntity(database="x", schema="dbo", name="v1", parse_status="sqlglot")

    deps = build_dependency_graph(stored_procedures=[proc], views=[view], packages=[], foreign_keys=[])
    edge = next(d for d in deps if d.target_object == "dbo.v1")
    assert edge.target_type == "view"


def test_unclassified_reference_defaults_to_table():
    proc = StoredProcedureEntity(
        database="x", schema="dbo", name="usp_a", loc=1, referenced_tables=["dbo.SomeUnknownName"], parse_status="sqlglot",
    )
    deps = build_dependency_graph(stored_procedures=[proc], views=[], packages=[], foreign_keys=[])
    assert deps[0].target_type == "table"


def test_procedure_to_function_edge():
    proc = StoredProcedureEntity(
        database="x", schema="dbo", name="usp_a", loc=1, referenced_functions=["dbo.ufnFoo"], parse_status="sqlglot",
    )
    deps = build_dependency_graph(stored_procedures=[proc], views=[], packages=[], foreign_keys=[])
    assert len(deps) == 1
    assert deps[0].source_type == "stored_procedure"
    assert deps[0].target_type == "function"
    assert deps[0].relationship_type == "calls"


def test_view_to_function_edge():
    view = ViewEntity(database="x", schema="dbo", name="v1", referenced_functions=["dbo.ufnFoo"], parse_status="sqlglot")
    deps = build_dependency_graph(stored_procedures=[], views=[view], packages=[], foreign_keys=[])
    assert len(deps) == 1
    assert deps[0].source_type == "view"
    assert deps[0].target_type == "function"


def test_function_to_table_and_function_to_function_edges():
    func = FunctionEntity(
        database="x", schema="dbo", name="ufnA", function_type="SCALAR",
        referenced_tables=["dbo.Orders"], referenced_functions=["dbo.ufnB"], parse_status="sqlglot",
    )
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], functions=[func])
    assert len(deps) == 2
    kinds = {(d.target_type, d.target_object) for d in deps}
    assert ("table", "dbo.Orders") in kinds
    assert ("function", "dbo.ufnB") in kinds


def test_trigger_fires_on_edge_uses_direct_metadata():
    trigger = TriggerEntity(database="x", schema="dbo", name="trg1", table="dbo.Orders", event="UPDATE")
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], triggers=[trigger])
    assert len(deps) == 1
    assert deps[0].relationship_type == "fires_on"
    assert deps[0].discovery_method == "direct_metadata"
    assert deps[0].target_object == "dbo.Orders"


def test_trigger_body_reads_calls_and_function_edges():
    trigger = TriggerEntity(
        database="x", schema="dbo", name="trg1", table="dbo.Orders", event="UPDATE",
        referenced_tables=["dbo.AuditLog"], referenced_procs=["dbo.usp_audit"],
        referenced_functions=["dbo.ufnNow"], parse_status="sqlglot",
    )
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], triggers=[trigger])
    kinds = {(d.relationship_type, d.target_type, d.target_object) for d in deps}
    assert ("fires_on", "table", "dbo.Orders") in kinds
    assert ("reads", "table", "dbo.AuditLog") in kinds
    assert ("calls", "stored_procedure", "dbo.usp_audit") in kinds
    assert ("calls", "function", "dbo.ufnNow") in kinds


def test_trigger_fires_on_and_body_reference_to_same_table_are_deduplicated():
    """fires_on and reads are DIFFERENT relationship types (both facts are
    kept), but a duplicate within the SAME relationship type must collapse."""
    trigger = TriggerEntity(
        database="x", schema="dbo", name="trg1", table="dbo.Orders", event="UPDATE",
        referenced_tables=["dbo.Orders", "dbo.Orders"], parse_status="sqlglot",  # duplicate reference
    )
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], triggers=[trigger])
    reads_edges = [d for d in deps if d.relationship_type == "reads"]
    assert len(reads_edges) == 1  # the duplicate "reads" entry collapsed
    fires_on_edges = [d for d in deps if d.relationship_type == "fires_on"]
    assert len(fires_on_edges) == 1  # distinct relationship_type, both kept


def test_constraint_to_table_and_function_edges():
    constraint = ConstraintEntity(
        database="x", schema="dbo", table="Orders", name="CK_x", constraint_type="CHECK",
        definition="(dbo.ufnIsValid([TotalDue]))", referenced_functions=["dbo.ufnIsValid"], parse_status="sqlglot",
    )
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], constraints=[constraint])
    assert len(deps) == 1
    assert deps[0].source_object == "dbo.Orders.CK_x"
    assert deps[0].source_type == "constraint"
    assert deps[0].target_object == "dbo.ufnIsValid"


def test_constraint_without_definition_produces_no_edges():
    """PRIMARY_KEY/UNIQUE/FOREIGN_KEY constraints have no definition text --
    must not produce spurious edges even if referenced_tables/functions
    somehow got populated."""
    constraint = ConstraintEntity(
        database="x", schema="dbo", table="Orders", name="PK_Orders", constraint_type="PRIMARY_KEY",
        columns=["OrderId"],
    )
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], constraints=[constraint])
    assert deps == []


def test_synonym_to_base_object_edge():
    synonym = SynonymEntity(database="x", schema="dbo", name="OrdersAlias", base_object="dbo.Orders")
    deps = build_dependency_graph(stored_procedures=[], views=[], packages=[], foreign_keys=[], synonyms=[synonym])
    assert len(deps) == 1
    assert deps[0].source_type == "synonym"
    assert deps[0].relationship_type == "references"
    assert deps[0].target_object == "dbo.Orders"


def test_synonym_to_view_base_object_is_classified_as_view():
    view = ViewEntity(database="x", schema="dbo", name="v1")
    synonym = SynonymEntity(database="x", schema="dbo", name="ViewAlias", base_object="dbo.v1")
    deps = build_dependency_graph(stored_procedures=[], views=[view], packages=[], foreign_keys=[], synonyms=[synonym])
    assert deps[0].target_type == "view"


def test_no_new_entities_produces_only_existing_edge_types():
    """Omitting all new optional params entirely (functions/triggers/
    constraints/synonyms) must behave exactly as before -- no crash, no
    unexpected edges."""
    proc = StoredProcedureEntity(database="x", schema="dbo", name="usp_a", loc=1, referenced_tables=["dbo.Orders"], parse_status="sqlglot")
    deps = build_dependency_graph(stored_procedures=[proc], views=[], packages=[], foreign_keys=[])
    assert len(deps) == 1
    assert deps[0].source_type == "stored_procedure"
