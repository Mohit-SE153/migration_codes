"""
Registry and entry point for Lakebridge's independent SQL Server
catalog-metadata dependency discovery.

This package is the fourth dependency source in the Lakebridge Discovery
pipeline (see orchestrator.py), running strictly after:
  1. report_parser.py's Analyzer-native extraction (discovery_method="lakebridge_report")
  2. dependency_extractor.py's regex gap-fill (discovery_method="lakebridge")
Neither of those is imported, called, or modified by this package -- it only
ever *appends* edges to the same result.dependencies list, tagged
discovery_method="catalog_metadata" (see vocabulary.py) so they stay fully
distinguishable in dependencies.json and dependency_stats.json.

Independent of SQLGlot/autovista/discovery_comparison: every probe this
package runs queries SQL Server catalog views directly over this package's
own connection (connection.py) -- it never imports autovista.*, never reads
SQLGlot's output files, and is never called from anywhere outside
lakebridge_discovery.orchestrator.

Extending this package: add one new probe module (one file, one dependency
or inventory category), then add one line to _REGISTRY below.
orchestrator.py never needs to change again -- see this module's own
docstring history for the plan (foreign_keys.py, user_defined_types.py,
xml_schema_collections.py, computed_column_functions.py, indexes.py,
constraints.py, sequences.py now; partition_functions.py,
security_objects.py later). The first four populate result.dependencies;
indexes.py/constraints.py/sequences.py are pure object-inventory probes
that populate result.indexes/.constraints/.sequences instead and emit no
dependency edges at all -- the registry doesn't care which of a probe's
fields it touches, only that it follows the same (connection, result,
seen_edges) signature.

A probe has the signature `(connection, result, seen_edges) -> None`: it
queries `connection` (one shared pyodbc connection per run, opened by this
module), appends any new LakebridgeDependencyRef it finds to
`result.dependencies`, and consults/updates the shared `seen_edges` set
(`{(source_object, target_object, relationship_type)}`) so it never
duplicates an edge any prior pass -- Analyzer-native, regex gap-fill, or an
earlier probe in this same run -- already produced.
"""
from __future__ import annotations

import dataclasses
from collections import Counter
from typing import Callable

from lakebridge_discovery.catalog_metadata import (
    computed_column_functions,
    constraints,
    foreign_keys,
    indexes,
    schemas,
    sequences,
    synonyms,
    user_defined_types,
    xml_schema_collections,
)
from lakebridge_discovery.catalog_metadata.connection import connect
from lakebridge_discovery.config import LakebridgeConfig
from lakebridge_discovery.logging_setup import logger
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult

CatalogProbe = Callable[[object, LakebridgeDiscoveryResult, set], None]

# Explicit, hand-written registry -- no dynamic plugin discovery/directory
# scanning. Each entry is (name, probe_function); `name` doubles as the
# LAKEBRIDGE_CATALOG_METADATA_SOURCES allowlist key. Adding a future probe
# (indexes, constraints, sequences, partition_functions, security_objects,
# ...) means one new file + one new line here -- orchestrator.py never
# changes again.
_REGISTRY: list[tuple[str, CatalogProbe]] = [
    (foreign_keys.NAME, foreign_keys.discover),
    (user_defined_types.NAME, user_defined_types.discover),
    (xml_schema_collections.NAME, xml_schema_collections.discover),
    (computed_column_functions.NAME, computed_column_functions.discover),
    # Object-inventory probes (populate result.indexes/.constraints/.sequences,
    # never result.dependencies) -- same registry, same connection/failure-
    # isolation machinery, just a different LakebridgeDiscoveryResult field.
    (indexes.NAME, indexes.discover),
    (constraints.NAME, constraints.discover),
    (sequences.NAME, sequences.discover),
    (schemas.NAME, schemas.discover),
    (synonyms.NAME, synonyms.discover),
]


def _select_active_probes(sources_config: str) -> list[tuple[str, CatalogProbe]]:
    """Parses LAKEBRIDGE_CATALOG_METADATA_SOURCES: "*" (default) selects
    every registered probe, "" or "none" selects none, otherwise a
    comma-separated allowlist of probe names. Pure function, no I/O -- unit
    testable without a database or a populated registry."""
    normalized = (sources_config or "").strip().lower()
    if normalized in ("", "none"):
        return []
    if normalized == "*":
        return list(_REGISTRY)
    wanted = {name.strip() for name in normalized.split(",") if name.strip()}
    return [(name, probe) for name, probe in _REGISTRY if name in wanted]


def _compute_stats(dependencies: list[LakebridgeDependencyRef]) -> dict:
    """Recomputes the same shape dependency_extractor.py's own (private)
    _stats() produces, but over the FULL dependency list -- Analyzer-native +
    regex gap-fill + catalog metadata combined. Deliberately reimplemented
    here rather than importing dependency_extractor._stats, so this package
    never depends on that module's internals and dependency_extractor.py
    never has to change. Called unconditionally at the end of run() (even
    when no probe added anything), so result.dependency_stats always
    reflects every pass that has completed by the time outputs are written."""
    by_relationship: Counter = Counter(d.relationship_type for d in dependencies)
    by_type_pair: Counter = Counter(f"{d.source_type}->{d.target_type}" for d in dependencies)
    by_discovery_method: Counter = Counter(d.discovery_method for d in dependencies)
    unique_relationships = {(d.source_object, d.target_object, d.relationship_type) for d in dependencies}
    resolved = sum(1 for d in dependencies if d.resolved)
    return {
        "total_dependencies": len(dependencies),
        "unique_relationships": len(unique_relationships),
        "by_relationship_type": dict(by_relationship),
        "by_type_pair": dict(sorted(by_type_pair.items())),
        "by_discovery_method": dict(by_discovery_method),
        "resolved": resolved,
        "unresolved": len(dependencies) - resolved,
    }


def _count_all_lists(result: LakebridgeDiscoveryResult) -> int:
    """Sums every list-typed field on `result` (tables, dependencies,
    indexes, constraints, sequences, ...), not just result.dependencies --
    needed because not every probe touches the dependency list: indexes.py/
    constraints.py/sequences.py are pure object-inventory probes that only
    grow result.indexes/.constraints/.sequences. Using this generic,
    field-introspecting count (rather than hand-picking specific fields)
    means a future inventory-only probe (partition_functions, security
    objects, ...) is automatically reflected here too, with no edit needed."""
    return sum(
        len(value) for f in dataclasses.fields(result)
        if isinstance(value := getattr(result, f.name), list)
    )


def _run_active_probes(config: LakebridgeConfig, result: LakebridgeDiscoveryResult) -> None:
    active_probes = _select_active_probes(config.catalog_metadata_sources)
    if config.run_mode != "live" or not active_probes:
        logger.info(
            "SKIP catalog_metadata discovery: run_mode=%r active_probes=%d/%d registered",
            config.run_mode, len(active_probes), len(_REGISTRY),
        )
        return

    seen_edges: set[tuple] = {(d.source_object, d.target_object, d.relationship_type) for d in result.dependencies}

    try:
        connection = connect(config)
    except Exception as exc:  # noqa: BLE001 - a connection failure must not abort the whole discovery run
        msg = f"catalog_metadata: could not establish SQL Server connection: {type(exc).__name__}: {exc}"
        result.warnings.append(msg)
        logger.error(msg)
        return

    try:
        for name, probe in active_probes:
            try:
                before = _count_all_lists(result)
                probe(connection, result, seen_edges)
                logger.info("OK   catalog_metadata probe=%-24s new_objects=%d", name, _count_all_lists(result) - before)
            except Exception as exc:  # noqa: BLE001 - one bad probe must not take down the others
                msg = f"catalog_metadata probe '{name}' failed: {type(exc).__name__}: {exc}"
                result.warnings.append(msg)
                logger.error(msg)
    finally:
        connection.close()


def run(config: LakebridgeConfig, result: LakebridgeDiscoveryResult) -> None:
    """Runs every active catalog-metadata probe against one shared SQL
    Server connection, then unconditionally recomputes result.dependency_stats.
    Never raises: a disabled/fixture-mode run, a connection failure, or a
    single probe's exception are all recorded as a log line/warning rather
    than aborting the rest of the Lakebridge Discovery run, matching this
    engine's existing defensive style (source_exporter.py, report_parser.py)."""
    try:
        _run_active_probes(config, result)
    finally:
        result.dependency_stats = _compute_stats(result.dependencies)
