"""
Writes Lakebridge Discovery's output contract. Mirrors the shape of
autovista/output_writer.py (per-category JSON files + a manifest + a CSV
rollup + a log summary) for a similar developer experience, but is fully
independent code writing into its own output directory
(LAKEBRIDGE_OUTPUT_DIR, default ./output_lakebridge) -- never
autovista's ./output.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from lakebridge_discovery.logging_setup import logger
from lakebridge_discovery.schema import LakebridgeDiscoveryResult, LakebridgeLogEntry

ENTITY_OUTPUT_FILES = {
    "tables": "tables.json",
    "views": "views.json",
    "stored_procedures": "stored_procedures.json",
    "functions": "functions.json",
    "triggers": "triggers.json",
    "synonyms": "synonyms.json",
    "schemas": "schemas.json",
    "packages": "packages.json",
    "indexes": "indexes.json",
    "constraints": "constraints.json",
    "sequences": "sequences.json",
    "unsupported_objects": "unsupported_objects.json",
    "dependencies": "dependencies.json",
}


def write_entity_outputs(result: LakebridgeDiscoveryResult, output_dir: str) -> dict[str, Path]:
    result_dict = result.to_dict()
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}
    for field_name, filename in ENTITY_OUTPUT_FILES.items():
        out_path = out_dir / filename
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result_dict[field_name], f, indent=2, default=str)
        paths[field_name] = out_path

    logger.info("Wrote %d per-category output files to %s", len(paths), out_dir)
    return paths


def write_manifest_json(result: LakebridgeDiscoveryResult, output_dir: str, filename: str = "lakebridge_manifest.json") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, default=str)
    return out_path


def write_csv_rollup(result: LakebridgeDiscoveryResult, output_dir: str, filename: str = "lakebridge_rollup.csv") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = [
        {"object_type": "table", "object_name": "(all)", "count": len(result.tables)},
        {"object_type": "view", "object_name": "(all)", "count": len(result.views)},
        {"object_type": "stored_procedure", "object_name": "(all)", "count": len(result.stored_procedures)},
        {"object_type": "function", "object_name": "(all)", "count": len(result.functions)},
        {"object_type": "trigger", "object_name": "(all)", "count": len(result.triggers)},
        {"object_type": "synonym", "object_name": "(all)", "count": len(result.synonyms)},
        {"object_type": "schema", "object_name": "(all)", "count": len(result.schemas)},
        {"object_type": "ssis_package", "object_name": "(all)", "count": len(result.packages)},
        {"object_type": "index", "object_name": "(all)", "count": len(result.indexes)},
        {"object_type": "constraint", "object_name": "(all)", "count": len(result.constraints)},
        {"object_type": "sequence", "object_name": "(all)", "count": len(result.sequences)},
        {"object_type": "unsupported_object", "object_name": "(all)", "count": len(result.unsupported_objects)},
        {"object_type": "dependency_edge", "object_name": "(all)", "count": len(result.dependencies)},
        {"object_type": "warning", "object_name": "(all)", "count": len(result.warnings)},
        {"object_type": "error", "object_name": "(all)", "count": len(result.errors)},
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["object_type", "object_name", "count"])
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def write_dependency_stats(result: LakebridgeDiscoveryResult, output_dir: str, filename: str = "dependency_stats.json") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result.dependency_stats, f, indent=2, default=str)
    return out_path


def write_run_log_summary(log_entries: list[LakebridgeLogEntry], output_dir: str, filename: str = "lakebridge_log_summary.csv") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["stage", "object_type", "object_name", "status", "error", "duration_ms"])
        writer.writeheader()
        for entry in log_entries:
            writer.writerow({
                "stage": entry.stage, "object_type": entry.object_type, "object_name": entry.object_name,
                "status": entry.status, "error": entry.error or "", "duration_ms": entry.duration_ms,
            })
    return out_path
