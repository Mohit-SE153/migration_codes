"""
Writes the Discovery-phase output contract.

Enhancement 5: per-object-category is now its own primary JSON output file
(database.json, tables.json, constraints.json, ...) instead of one single
nested manifest. discovery_manifest.json is kept for backward compatibility
-- write_manifest_json() assembles it FROM the per-category files (not the
other way around), so the per-category files are the source of truth and
the aggregate manifest is provably a faithful combination of them.

Also still written: a flat CSV rollup for quick human sanity-checking, and
the per-object run log.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from autovista.logging_setup import logger
from autovista.schema import DiscoveryLogEntry, DiscoveryManifest

# DiscoveryManifest field name -> output filename. Covers every field on
# DiscoveryManifest so write_manifest_json can assemble a complete,
# byte-for-byte-equivalent aggregate from these files alone.
ENTITY_OUTPUT_FILES = {
    "server_instance": "server_instance.json",
    "databases": "database.json",
    "database_summary": "database_summary.json",
    "tables": "tables.json",
    "views": "views.json",
    "stored_procedures": "stored_procedures.json",
    "functions": "functions.json",
    "triggers": "triggers.json",
    "indexes": "indexes.json",
    "constraints": "constraints.json",
    "agent_jobs": "agent_jobs.json",
    "packages": "packages.json",
    "dependencies": "dependencies.json",
    "permissions": "permissions.json",
    "security_principals": "security_principals.json",
    "linked_servers": "linked_servers.json",
    "synonyms": "synonyms.json",
    "sequences": "sequences.json",
    "assemblies": "assemblies.json",
    "xml_schema_collections": "xml_schema_collections.json",
    "user_defined_types": "user_defined_types.json",
    "database_files": "database_files.json",
    "data_quality_summary": "data_quality_summary.json",
}


def write_entity_outputs(manifest: DiscoveryManifest, output_dir: str) -> dict[str, Path]:
    """Writes each Discovery object category to its own JSON file -- the
    primary Discovery outputs as of Enhancement 5. Returns a dict of
    manifest-field-name -> path written (plus "foreign_keys", a bonus
    derived file, see below)."""
    manifest_dict = manifest.to_dict()
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}
    for field_name, filename in ENTITY_OUTPUT_FILES.items():
        out_path = out_dir / filename
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(manifest_dict[field_name], f, indent=2, default=str)
        paths[field_name] = out_path

    # foreign_keys.json is a bonus derived view (the FOREIGN_KEY-typed rows
    # from constraints.json) -- not its own DiscoveryManifest field.
    # constraints.json remains the complete/authoritative constraint list.
    fk_path = out_dir / "foreign_keys.json"
    foreign_key_constraints = [c for c in manifest_dict["constraints"] if c.get("constraint_type") == "FOREIGN_KEY"]
    with open(fk_path, "w", encoding="utf-8") as f:
        json.dump(foreign_key_constraints, f, indent=2, default=str)
    paths["foreign_keys"] = fk_path

    logger.info("Wrote %d per-category output files to %s", len(paths), out_dir)
    return paths


def write_manifest_json(manifest: DiscoveryManifest, output_dir: str, filename: str = "discovery_manifest.json") -> Path:
    """Backward-compatible aggregate output -- same shape as before
    Enhancement 5. Assembled FROM the per-category files written by
    write_entity_outputs() rather than from manifest.to_dict() directly."""
    entity_paths = write_entity_outputs(manifest, output_dir)

    assembled: dict = {}
    for field_name in ENTITY_OUTPUT_FILES:
        with open(entity_paths[field_name], "r", encoding="utf-8") as f:
            assembled[field_name] = json.load(f)

    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(assembled, f, indent=2, default=str)
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
        "object_type": "constraint", "object_name": "(all)", "count": len(manifest.constraints),
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

    # SQL-Server-feature compatibility scan (autovista/compatibility_scanner.py):
    # one row per distinct flag across every scanned object (stored procs,
    # views, functions, triggers, embedded SQL), so a reviewer can see e.g.
    # "3 objects use MERGE" without opening the manifest.
    flag_counts: dict[str, int] = {}
    for collection in (manifest.stored_procedures, manifest.views, manifest.functions, manifest.triggers):
        for obj in collection:
            for flag in obj.compatibility_flags:
                flag_counts[flag] = flag_counts.get(flag, 0) + 1
    for pkg in manifest.packages:
        for embedded in pkg.embedded_sql:
            for flag in embedded.compatibility_flags:
                flag_counts[flag] = flag_counts.get(flag, 0) + 1
    for flag_name, count in sorted(flag_counts.items()):
        rows.append({
            "object_type": "compatibility_flag", "object_name": flag_name, "count": count,
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
