"""
Shared object-name lookup helper for catalog_metadata's probes.

Extracted out of user_defined_types.py (which needed it in its general
form, scanning more than one inventory category) once a third probe
(xml_schema_collections.py) needed the identical one-category form
foreign_keys.py had already hand-written separately -- three near-identical
copies of the same seven lines was the point where sharing this stopped
being premature. No probe's query logic, output shape, or casing behavior
changes as a result of this extraction.
"""
from __future__ import annotations

from lakebridge_discovery.schema import LakebridgeDiscoveryResult


def name_by_key(result: LakebridgeDiscoveryResult, *categories: str) -> dict[str, str]:
    """lower("schema.name") -> this run's own inventory casing, scanning one
    or more of result's object-inventory categories (e.g. "tables", or
    "stored_procedures"/"functions" together for a routine lookup). Used so
    a catalog-sourced edge's source_object matches the exact casing that
    object already has elsewhere in this run's dependencies.json, rather
    than whatever casing a given catalog query happened to return."""
    names: dict[str, str] = {}
    for category in categories:
        for obj in getattr(result, category):
            if "." in obj.name:
                schema, _, bare = obj.name.rpartition(".")
                names[f"{schema.lower()}.{bare.lower()}"] = obj.name
    return names
