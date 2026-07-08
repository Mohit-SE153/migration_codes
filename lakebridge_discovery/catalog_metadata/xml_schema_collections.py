"""
Table -> XML Schema Collection dependency discovery, from SQL Server
catalog metadata only (sys.columns / sys.xml_schema_collections /
sys.schemas) -- no SQL parsing, no dependence on the Analyzer report or
dependency_extractor.py's regex gap-fill.

sys.columns.xml_collection_id is the column-level binding SQL Server itself
maintains for any XML column typed against a schema collection
("col XML(schema_collection)"); 0 (or NULL, depending on server version)
means the column is untyped XML -- or not an XML column at all -- and is
excluded here via "xml_collection_id > 0".

Scoped to sys.tables only (not views): XML-schema-collection typing is a
physical-column property, same scoping decision already made for
user_defined_types.py's table-side query.

Procedure/function parameters can also be typed against an XML schema
collection (sys.parameters.xml_collection_id, mirroring sys.columns) -- out
of scope here, since this stage's brief names only sys.columns/
sys.xml_schema_collections; a future probe could add routine-parameter
coverage the same way user_defined_types.py added it for UDTs, if ever
needed.

DISTINCT collapses multiple XML columns on the same table typed against the
same collection to one edge at the SQL level; seen_edges is still checked
per-row as a second, cheap layer of defense, same convention as the other
probes in this package.
"""
from __future__ import annotations

from lakebridge_discovery.catalog_metadata import vocabulary
from lakebridge_discovery.catalog_metadata.naming import name_by_key
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult, LakebridgeObjectRef

NAME = "xml_schema_collections"

_QUERY_TABLE_XML_SCHEMA_COLLECTION = """
SELECT DISTINCT
    ts.name AS table_schema, t.name AS table_name,
    xss.name AS collection_schema, xsc.name AS collection_name
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.schemas ts ON ts.schema_id = t.schema_id
JOIN sys.xml_schema_collections xsc ON xsc.xml_collection_id = c.xml_collection_id
JOIN sys.schemas xss ON xss.schema_id = xsc.schema_id
WHERE c.xml_collection_id > 0
ORDER BY ts.name, t.name, xss.name, xsc.name
"""

# Distinct COLLECTION *objects* (e.g. "Person.AdditionalContactInfoSchemaCollection")
# -- separate from, and much smaller than, the uses_type *dependency edge*
# count the query above already produces. Added for parity with autovista's
# own xml_schema_collections inventory list.
_QUERY_COLLECTION_INVENTORY = """
SELECT s.name AS schema_name, xsc.name AS collection_name
FROM sys.xml_schema_collections xsc
JOIN sys.schemas s ON s.schema_id = xsc.schema_id
WHERE xsc.name <> 'sys'  -- exclude the built-in "sys" collection every database has
ORDER BY s.name, xsc.name
"""


def _discover_collection_inventory(connection, result: LakebridgeDiscoveryResult) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_COLLECTION_INVENTORY)
    seen_names: set[str] = set()
    for schema_name, collection_name in cursor.fetchall():
        name = f"{schema_name}.{collection_name}"
        if name in seen_names:
            continue
        seen_names.add(name)
        result.xml_schema_collections.append(LakebridgeObjectRef(
            object_type="xml_schema_collection",
            name=name,
            source_tech="MS SQL Server",
            raw_category="sys.xml_schema_collections",
        ))


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    table_names = name_by_key(result, "tables")

    cursor = connection.cursor()
    cursor.execute(_QUERY_TABLE_XML_SCHEMA_COLLECTION)
    rows = cursor.fetchall()

    for table_schema, table_name, collection_schema, collection_name in rows:
        key = f"{table_schema.lower()}.{table_name.lower()}"
        source_object = table_names.get(key, f"{table_schema}.{table_name}")
        target_object = f"{collection_schema.lower()}.{collection_name.lower()}"

        edge_key = (source_object, target_object, vocabulary.USES_TYPE)
        if edge_key in seen_edges:
            continue
        seen_edges.add(edge_key)

        result.dependencies.append(LakebridgeDependencyRef(
            source_object=source_object,
            target_object=target_object,
            relationship_type=vocabulary.USES_TYPE,
            raw_category=vocabulary.RAW_CATEGORY_XML_SCHEMA_COLLECTION,
            source_type=vocabulary.TABLE,
            target_type=vocabulary.XML_SCHEMA_COLLECTION,
            discovery_method=vocabulary.DISCOVERY_METHOD,
            resolved=True,
        ))

    _discover_collection_inventory(connection, result)
