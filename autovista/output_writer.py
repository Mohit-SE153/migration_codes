"""
Writes the Discovery-phase output contract: one nested JSON manifest
(single file -- simpler for the Assessment phase to consume than
juggling N normalized files, and small-pilot scale doesn't need the
split) plus a flat CSV rollup for quick human sanity-checking, and the
per-object run log.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from autovista.schema import DiscoveryLogEntry, DiscoveryManifest


def write_manifest_json(manifest: DiscoveryManifest, output_dir: str, filename: str = "discovery_manifest.json") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, indent=2, default=str)
    return out_path


def write_csv_rollup(manifest: DiscoveryManifest, output_dir: str, filename: str = "discovery_rollup.csv") -> Path:
    """Counts and sizes only -- meant to be opened in Excel by someone
    who wants a 30-second sanity check, not the full graph."""
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for db in manifest.databases:
        rows.append({
            "object_type": "database", "object_name": db.name, "count": 1,
            "size_mb": db.size_mb, "tables": db.table_count, "procs": db.proc_count, "views": db.view_count,
        })
    rows.append({
        "object_type": "table", "object_name": "(all)", "count": len(manifest.tables),
        "size_mb": round(sum(t.size_mb for t in manifest.tables), 2),
        "tables": "", "procs": "", "views": "",
    })
    rows.append({
        "object_type": "stored_procedure", "object_name": "(all)", "count": len(manifest.stored_procedures),
        "size_mb": "", "tables": "", "procs": "", "views": "",
    })
    rows.append({
        "object_type": "database_file", "object_name": "(all)", "count": len(manifest.database_files),
        "size_mb": "", "tables": "", "procs": "", "views": "",
    })
    rows.append({
        "object_type": "index", "object_name": "(all)", "count": len(manifest.indexes),
        "size_mb": "", "tables": "", "procs": "", "views": "",
    })
    rows.append({
        "object_type": "function", "object_name": "(all)", "count": len(manifest.functions),
        "size_mb": "", "tables": "", "procs": "", "views": "",
    })
    rows.append({
        "object_type": "synonym", "object_name": "(all)", "count": len(manifest.synonyms),
        "size_mb": "", "tables": "", "procs": "", "views": "",
    })
    rows.append({
        "object_type": "sequence", "object_name": "(all)", "count": len(manifest.sequences),
        "size_mb": "", "tables": "", "procs": "", "views": "",
    })
    rows.append({
        "object_type": "view", "object_name": "(all)", "count": len(manifest.views),
        "size_mb": "", "tables": "", "procs": "", "views": "",
    })
    rows.append({
        "object_type": "ssis_package", "object_name": "(all)", "count": len(manifest.packages),
        "size_mb": "", "tables": "", "procs": "", "views": "",
    })
    rows.append({
        "object_type": "dependency_edge", "object_name": "(all)", "count": len(manifest.dependencies),
        "size_mb": "", "tables": "", "procs": "", "views": "",
    })
    # A proc/embedded-SQL object needs human review either because it's
    # explicitly unresolved/llm_inferred, or because parse_status stayed
    # "sqlglot" but unresolved_reason is non-null -- e.g. sqlglot fell
    # back to an opaque Command node for part of the body (nested
    # BEGIN/END depth, full-text search predicates, etc.), meaning
    # referenced_tables may be incomplete even though *some* references
    # were confidently extracted. Both cases are equally "don't treat
    # this as complete ground truth."
    needs_review = sum(
        1 for p in manifest.stored_procedures
        if p.parse_status == "unresolved" or p.unresolved_reason
    ) + sum(
        1 for pkg in manifest.packages for e in pkg.embedded_sql
        if e.parse_status in ("unresolved", "llm_inferred") or e.unresolved_reason
    )
    rows.append({
        "object_type": "unresolved_or_llm_inferred", "object_name": "(needs human review)", "count": needs_review,
        "size_mb": "", "tables": "", "procs": "", "views": "",
    })

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["object_type", "object_name", "count", "size_mb", "tables", "procs", "views"])
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def write_run_log_summary(log_entries: list[DiscoveryLogEntry], output_dir: str, filename: str = "discovery_log_summary.csv") -> Path:
    """Per-object success/failure, not just aggregate counts -- so a
    failed parse is individually triageable without grepping the raw log."""
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["object_type", "object_name", "status", "parse_status", "error", "duration_ms"])
        writer.writeheader()
        for entry in log_entries:
            writer.writerow({
                "object_type": entry.object_type, "object_name": entry.object_name,
                "status": entry.status, "parse_status": entry.parse_status or "",
                "error": entry.error or "", "duration_ms": entry.duration_ms,
            })
    return out_path
