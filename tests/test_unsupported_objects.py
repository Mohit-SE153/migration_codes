"""
Tests for autovista.unsupported_objects.collect_unsupported_objects --
derives its list purely from parse_status/unresolved_reason fields entities
already carry, never a new parsing pass.
"""
from __future__ import annotations

from autovista.schema import (
    ConstraintEntity,
    DiscoveryManifest,
    EmbeddedSqlEntity,
    FunctionEntity,
    PackageEntity,
    StoredProcedureEntity,
    TriggerEntity,
    ViewEntity,
)
from autovista.unsupported_objects import collect_unsupported_objects


def test_no_unresolved_objects_produces_empty_list():
    manifest = DiscoveryManifest()
    manifest.stored_procedures = [
        StoredProcedureEntity(database="SalesDW", schema="dbo", name="usp_Clean", loc=10, parse_status="sqlglot"),
    ]
    assert collect_unsupported_objects(manifest) == []


def test_unresolved_stored_procedure_is_collected():
    manifest = DiscoveryManifest()
    manifest.stored_procedures = [
        StoredProcedureEntity(
            database="SalesDW", schema="dbo", name="usp_Weird", loc=5,
            parse_status="unresolved", unresolved_reason="nested dynamic SQL depth exceeded",
        ),
    ]
    unsupported = collect_unsupported_objects(manifest)
    assert len(unsupported) == 1
    obj = unsupported[0]
    assert obj.object_type == "stored_procedure"
    assert obj.name == "dbo.usp_Weird"
    assert obj.parse_status == "unresolved"
    assert obj.reason == "nested dynamic SQL depth exceeded"


def test_view_with_unresolved_reason_but_non_unresolved_parse_status_is_still_collected():
    """A degraded-but-not-fully-failed parse (parse_status stayed 'sqlglot'
    but unresolved_reason is set) still needs human review -- same
    condition the existing rollup's 'unresolved_or_llm_inferred' row uses."""
    manifest = DiscoveryManifest()
    manifest.views = [
        ViewEntity(database="SalesDW", schema="dbo", name="vComplex", parse_status="sqlglot", unresolved_reason="opaque Command node"),
    ]
    unsupported = collect_unsupported_objects(manifest)
    assert len(unsupported) == 1
    assert unsupported[0].object_type == "view"
    assert unsupported[0].name == "dbo.vComplex"


def test_collects_across_functions_triggers_and_constraints():
    manifest = DiscoveryManifest()
    manifest.functions = [FunctionEntity(database="SalesDW", schema="dbo", name="ufnBad", function_type="SCALAR", parse_status="unresolved")]
    manifest.triggers = [TriggerEntity(database="SalesDW", schema="dbo", name="trgBad", table="Orders", event="UPDATE", parse_status="unresolved")]
    manifest.constraints = [ConstraintEntity(database="SalesDW", schema="dbo", table="Orders", name="CK_Bad", constraint_type="CHECK", parse_status="unresolved")]

    unsupported = collect_unsupported_objects(manifest)
    types = {u.object_type for u in unsupported}
    assert types == {"function", "trigger", "constraint"}


def test_embedded_sql_llm_inferred_is_collected():
    manifest = DiscoveryManifest()
    package = PackageEntity(name="LoadOrders", project="ETL", deployment_model="ssisdb")
    package.embedded_sql = [
        EmbeddedSqlEntity(task_name="Execute SQL Task", task_type="SQLTask", sql_text="EXEC dbo.usp_Weird", parse_status="llm_inferred"),
    ]
    manifest.packages = [package]

    unsupported = collect_unsupported_objects(manifest)
    assert len(unsupported) == 1
    assert unsupported[0].object_type == "embedded_sql"
    assert unsupported[0].name == "ETL.LoadOrders::Execute SQL Task"


def test_resolved_direct_metadata_objects_are_never_collected():
    manifest = DiscoveryManifest()
    manifest.stored_procedures = [StoredProcedureEntity(database="SalesDW", schema="dbo", name="usp_Fine", loc=1, parse_status="direct_metadata")]
    manifest.views = [ViewEntity(database="SalesDW", schema="dbo", name="vFine", parse_status="sqlglot")]
    assert collect_unsupported_objects(manifest) == []
