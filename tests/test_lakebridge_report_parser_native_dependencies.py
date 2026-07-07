"""
Targeted tests for lakebridge_discovery.report_parser.extract_native_report_dependencies
-- the primary dependency source, pulling directly from Bladespector's own
`objectRel`/`subJobInfo` JSON fields (see report_parser.py's module
docstring for the schema references). dependency_extractor.py's regex scan
is only a gap-filler for whatever this leaves uncovered.
"""
from __future__ import annotations

from lakebridge_discovery.report_parser import extract_native_report_dependencies
from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeObjectRef


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


def test_extracts_reads_writes_calls_from_object_rel():
    data = {
        "inventory": [
            {
                "name": "view__Sales.vStoreWithDemographics.sql",
                "objectRel": [
                    {"object": "Sales.Store", "action": "read", "count": 1},
                    {"object": "Sales.Customer", "action": "read", "count": 1},
                ],
            },
            {
                "name": "sql_stored_procedure__dbo.uspGetStoreInfo.sql",
                "objectRel": [
                    {"object": "Sales.Store", "action": "write", "count": 1},
                    {"object": "dbo.uspLogError", "action": "execute", "count": 1},
                ],
            },
        ],
    }
    result = _base_result()
    extract_native_report_dependencies(data, result, "MS SQL Server")

    edges = {(d.source_object, d.target_object, d.relationship_type, d.discovery_method) for d in result.dependencies}
    assert ("Sales.vStoreWithDemographics", "sales.store", "reads", "lakebridge_report") in edges
    assert ("Sales.vStoreWithDemographics", "sales.customer", "reads", "lakebridge_report") in edges
    assert ("dbo.uspGetStoreInfo", "sales.store", "writes", "lakebridge_report") in edges
    assert ("dbo.uspGetStoreInfo", "dbo.usplogerror", "calls", "lakebridge_report") in edges


def test_unresolved_object_rel_target_is_marked_unresolved_not_dropped():
    data = {
        "inventory": [
            {
                "name": "sql_stored_procedure__dbo.uspGetStoreInfo.sql",
                "objectRel": [{"object": "dbo.SomeUnknownTable", "action": "read", "count": 2}],
            },
        ],
    }
    result = _base_result()
    extract_native_report_dependencies(data, result, "MS SQL Server")

    assert len(result.dependencies) == 1
    edge = result.dependencies[0]
    assert edge.target_object == "dbo.someunknowntable"
    assert edge.target_type == "unknown"
    assert edge.resolved is False


def test_create_and_drop_actions_are_not_emitted_as_edges():
    """create/drop are DDL on the object's own definition (typically a
    self-loop against the very object being defined) -- not a real
    cross-object dependency."""
    data = {
        "inventory": [
            {
                "name": "view__Sales.vStoreWithDemographics.sql",
                "objectRel": [
                    {"object": "Sales.vStoreWithDemographics", "action": "create", "count": 1},
                    {"object": "Sales.Store", "action": "drop", "count": 1},
                ],
            },
        ],
    }
    result = _base_result()
    extract_native_report_dependencies(data, result, "MS SQL Server")

    assert result.dependencies == []


def test_etl_nested_sql_statements_object_rel_is_extracted():
    data = {
        "inventory": [
            {
                "name": "sql_stored_procedure__dbo.uspGetStoreInfo.sql",
                "sqlStatements": [
                    {"nodeName": "SQ_1", "sql": "SELECT * FROM Sales.Store", "objectRel": [
                        {"object": "Sales.Store", "action": "source", "count": 1},
                    ]},
                ],
            },
        ],
    }
    result = _base_result()
    extract_native_report_dependencies(data, result, "SSIS")

    assert len(result.dependencies) == 1
    assert result.dependencies[0].target_object == "sales.store"
    assert result.dependencies[0].relationship_type == "reads"
    assert result.dependencies[0].raw_category == "sqlStatements.objectRel"


def test_sub_job_info_produces_calls_edges_between_packages():
    data = {
        "inventory": [],
        "subJobInfo": [
            {"parent": "MasterSequence", "parentType": "SEQUENCE", "child": "LoadStoreJob", "childType": "PARALLEL", "count": 1},
        ],
    }
    result = _base_result()
    extract_native_report_dependencies(data, result, "SSIS")

    assert len(result.dependencies) == 1
    edge = result.dependencies[0]
    assert edge.source_object == "MasterSequence"
    assert edge.target_object == "LoadStoreJob"
    assert edge.relationship_type == "calls"
    assert edge.source_type == "package"
    assert edge.target_type == "package"
    assert edge.raw_category == "subJobInfo"
    assert edge.resolved is True


def test_no_inventory_key_is_a_noop():
    result = _base_result()
    extract_native_report_dependencies({"runInfo": {}}, result, "MS SQL Server")
    assert result.dependencies == []
