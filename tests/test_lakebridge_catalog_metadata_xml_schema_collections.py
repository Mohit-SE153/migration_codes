"""
Tests for lakebridge_discovery.catalog_metadata.xml_schema_collections --
Table -> XML Schema Collection dependency discovery from
sys.columns/sys.xml_schema_collections/sys.schemas only. Exercised against a
stub connection/cursor (no real SQL Server), same spirit as the foreign_keys
and user_defined_types probe tests.
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import xml_schema_collections
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult, LakebridgeObjectRef


class _FakeCursor:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def execute(self, sql: str):
        return self

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


def _base_result() -> LakebridgeDiscoveryResult:
    result = LakebridgeDiscoveryResult()
    result.tables = [LakebridgeObjectRef(object_type="table", name="Person.Person", source_tech="MS SQL Server")]
    return result


def test_discover_emits_table_to_xml_schema_collection_edge():
    result = _base_result()
    connection = _FakeConnection([("Person", "Person", "Person", "AdditionalContactInfoSchemaCollection")])

    xml_schema_collections.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1
    edge = result.dependencies[0]
    assert edge.source_object == "Person.Person"
    assert edge.target_object == "person.additionalcontactinfoschemacollection"
    assert edge.relationship_type == "uses_type"
    assert edge.source_type == "table"
    assert edge.target_type == "xml_schema_collection"
    assert edge.discovery_method == "catalog_metadata"
    assert edge.raw_category == "sys.xml_schema_collections"
    assert edge.resolved is True


def test_discover_collapses_multiple_xml_columns_bound_to_same_collection():
    """A table with two XML columns typed against the same collection must
    still collapse to one edge -- what SELECT DISTINCT already prevents at
    the SQL level, exercised here as the seen_edges defense-in-depth."""
    result = _base_result()
    connection = _FakeConnection([
        ("Person", "Person", "Person", "AdditionalContactInfoSchemaCollection"),
        ("Person", "Person", "Person", "AdditionalContactInfoSchemaCollection"),
    ])

    xml_schema_collections.discover(connection, result, seen_edges=set())

    assert len(result.dependencies) == 1


def test_discover_two_different_collections_on_one_table_produce_two_edges():
    result = _base_result()
    connection = _FakeConnection([
        ("Person", "Person", "Person", "AdditionalContactInfoSchemaCollection"),
        ("Person", "Person", "Person", "IndividualSurveySchemaCollection"),
    ])

    xml_schema_collections.discover(connection, result, seen_edges=set())

    targets = {d.target_object for d in result.dependencies}
    assert targets == {"person.additionalcontactinfoschemacollection", "person.individualsurveyschemacollection"}


def test_discover_falls_back_to_catalog_casing_when_table_not_yet_in_inventory():
    result = LakebridgeDiscoveryResult()  # empty tables inventory
    connection = _FakeConnection([("Production", "ProductModel", "Production", "ProductDescriptionSchemaCollection")])

    xml_schema_collections.discover(connection, result, seen_edges=set())

    assert result.dependencies[0].source_object == "Production.ProductModel"
    assert result.dependencies[0].target_object == "production.productdescriptionschemacollection"


def test_discover_does_not_duplicate_an_edge_already_known_from_a_prior_pass():
    result = _base_result()
    existing = LakebridgeDependencyRef(
        source_object="Person.Person", target_object="person.additionalcontactinfoschemacollection",
        relationship_type="uses_type", source_type="table", target_type="xml_schema_collection",
        discovery_method="catalog_metadata", resolved=True,
    )
    result.dependencies.append(existing)
    seen_edges = {("Person.Person", "person.additionalcontactinfoschemacollection", "uses_type")}
    connection = _FakeConnection([("Person", "Person", "Person", "AdditionalContactInfoSchemaCollection")])

    xml_schema_collections.discover(connection, result, seen_edges)

    assert result.dependencies == [existing]


def test_xml_schema_collections_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "xml_schema_collections" in names
