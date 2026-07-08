"""
Collects manifest.warnings/manifest.errors from data this run already
produced -- never a new check or a fabricated count. Added for feature
parity with Lakebridge Discovery's own result.warnings/result.errors (see
lakebridge_discovery.report_parser/dependency_extractor/catalog_metadata,
which append to those lists at various non-fatal-vs-fatal points of their
own run).

errors: one string per DiscoveryLogEntry with status=="failed" -- this
engine's own existing hard-failure signal (see StateStore/orchestrator.py's
counters["failed"] and autovista/output_writer.py's existing "error" rollup
row, which already counts the identical condition; this just also exposes
it as a manifest-level list, matching Lakebridge's shape).

warnings: one string per entity that needed a fallback/degraded parse but
didn't hard-fail -- parse_status in ("unresolved", "llm_inferred") or a
non-null unresolved_reason, the same condition
autovista/output_writer.py's "unresolved_or_llm_inferred" rollup row and
unsupported_objects.py already use, just phrased here as a plain-English
message per object instead of a count.
"""
from __future__ import annotations

from autovista.schema import DiscoveryLogEntry, DiscoveryManifest

_LLM_INFERRED = "llm_inferred"
_UNRESOLVED = "unresolved"


def collect_errors(log_entries: list[DiscoveryLogEntry]) -> list[str]:
    return [
        f"{entry.object_type}:{entry.object_name}: {entry.error}"
        for entry in log_entries
        if entry.status == "failed" and entry.error
    ]


def collect_warnings(manifest: DiscoveryManifest) -> list[str]:
    warnings: list[str] = []

    def _add(object_type: str, name: str, parse_status, unresolved_reason: str | None) -> None:
        if parse_status in (_UNRESOLVED, _LLM_INFERRED) or unresolved_reason:
            reason = unresolved_reason or parse_status
            warnings.append(f"{object_type}:{name}: {reason}")

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
            _add("embedded_sql", f"{pkg.project}.{pkg.name}::{embedded.task_name}", embedded.parse_status, embedded.unresolved_reason)

    return warnings
