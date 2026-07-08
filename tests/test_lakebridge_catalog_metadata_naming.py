"""
Tests for the shared lakebridge_discovery.catalog_metadata.naming helper,
extracted in Stage 4 out of foreign_keys.py/user_defined_types.py's
previously-duplicated private lookup-dict builders.
"""
from __future__ import annotations

from lakebridge_discovery.catalog_metadata.naming import name_by_key
from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeObjectRef


def test_name_by_key_single_category():
    result = LakebridgeDiscoveryResult()
    result.tables = [LakebridgeObjectRef(object_type="table", name="Sales.SalesOrderHeader", source_tech="MS SQL Server")]

    names = name_by_key(result, "tables")

    assert names == {"sales.salesorderheader": "Sales.SalesOrderHeader"}


def test_name_by_key_multiple_categories_combined():
    result = LakebridgeDiscoveryResult()
    result.stored_procedures = [LakebridgeObjectRef(object_type="stored_procedure", name="dbo.uspLogError", source_tech="MS SQL Server")]
    result.functions = [LakebridgeObjectRef(object_type="function", name="dbo.ufnGetStock", source_tech="MS SQL Server")]

    names = name_by_key(result, "stored_procedures", "functions")

    assert names == {"dbo.usplogerror": "dbo.uspLogError", "dbo.ufngetstock": "dbo.ufnGetStock"}


def test_name_by_key_skips_names_without_a_schema():
    result = LakebridgeDiscoveryResult()
    result.tables = [LakebridgeObjectRef(object_type="table", name="UnqualifiedName", source_tech="MS SQL Server")]

    names = name_by_key(result, "tables")

    assert names == {}


def test_name_by_key_empty_category_list_returns_empty_dict():
    result = LakebridgeDiscoveryResult()
    result.tables = [LakebridgeObjectRef(object_type="table", name="Sales.Store", source_tech="MS SQL Server")]

    assert name_by_key(result) == {}
