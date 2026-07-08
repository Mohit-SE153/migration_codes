"""
Collects "this object could not be fully resolved" facts from entities this
run's own extractors/sqlglot lineage already produced -- never a new
parsing pass. Every DiscoveryManifest entity category that carries
parse_status/unresolved_reason (stored_procedures, views, functions,
triggers, constraints, and packages' embedded_sql) is scanned here; an
object is "unsupported" exactly when parse_status=="unresolved" or
unresolved_reason is set -- the identical condition
autovista/output_writer.py's write_csv_rollup already uses for its
"unresolved_or_llm_inferred" rollup row (embedded_sql additionally counts
parse_status=="llm_inferred", matching that same existing rollup logic).

Added for feature parity with Lakebridge Discovery's own
unsupported_objects.json (see lakebridge_discovery.report_parser, which
populates it from the Analyzer report's "unsupported" inventory category);
this is this engine's equivalent real signal, derived from data already on
the manifest per this task's "no fabricated/estimated values" constraint.
"""
from __future__ import annotations

from autovista.schema import DiscoveryManifest, UnsupportedObjectEntity

_LLM_INFERRED = "llm_inferred"
_UNRESOLVED = "unresolved"


def collect_unsupported_objects(manifest: DiscoveryManifest) -> list[UnsupportedObjectEntity]:
    unsupported: list[UnsupportedObjectEntity] = []

    def _add(object_type: str, name: str, parse_status, unresolved_reason: str | None) -> None:
        if parse_status == _UNRESOLVED or unresolved_reason:
            unsupported.append(UnsupportedObjectEntity(
                object_type=object_type, name=name, parse_status=parse_status, reason=unresolved_reason,
            ))

    for p in manifest.stored_procedures:
        _add("stored_procedure", f"{p.schema}.{p.name}", p.parse_status, p.unresolved_reason)
    for v in manifest.views:
        _add("view", f"{v.schema}.{v.name}", v.parse_status, v.unresolved_reason)
    for fn in manifest.functions:
        _add("function", f"{fn.schema}.{fn.name}", fn.parse_status, fn.unresolved_reason)
    for t in manifest.triggers:
        _add("trigger", f"{t.schema}.{t.name}", t.parse_status, t.unresolved_reason)
    for c in manifest.constraints:
        _add("constraint", f"{c.schema}.{c.table}.{c.name}", c.parse_status, c.unresolved_reason)

    for pkg in manifest.packages:
        for embedded in pkg.embedded_sql:
            if embedded.parse_status in (_UNRESOLVED, _LLM_INFERRED) or embedded.unresolved_reason:
                unsupported.append(UnsupportedObjectEntity(
                    object_type="embedded_sql",
                    name=f"{pkg.project}.{pkg.name}::{embedded.task_name}",
                    parse_status=embedded.parse_status,
                    reason=embedded.unresolved_reason,
                ))

    return unsupported
