"""
Defensive mapper: Lakebridge Analyzer report (JSON preferred, Excel
fallback) -> lakebridge_discovery.schema.LakebridgeDiscoveryResult.

The Analyzer's report schema is not publicly documented at the field
level (see schema.py's module docstring), so this parser does keyword-based
best-effort classification rather than assuming exact key/sheet names, and
never raises on an unrecognized shape -- it logs a warning onto the result
and moves on. Re-validate `_CATEGORY_KEYWORDS` and `_NAME_KEYS` below
against a real report the first time this runs against an actual
Databricks workspace, and tighten the mapping then.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lakebridge_discovery.logging_setup import logger
from lakebridge_discovery.schema import (
    AnalyzeInvocationEntity,
    LakebridgeDependencyRef,
    LakebridgeDiscoveryResult,
    LakebridgeObjectRef,
)

# Ordered so more specific keywords (e.g. "stored procedure") are checked
# before generic ones. Matched against lower-cased sheet names / JSON key
# paths / row "type"-ish field values.
_CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("dependenc", "dependency"),
    ("unsupported", "unsupported"),
    ("stored procedure", "stored_procedure"),
    ("procedure", "stored_procedure"),
    ("proc", "stored_procedure"),
    ("view", "view"),
    ("function", "function"),
    ("trigger", "trigger"),
    ("synonym", "synonym"),
    ("schema", "schema"),
    ("package", "package"),
    ("workflow", "package"),
    ("job", "package"),
    ("table", "table"),
]

_NAME_KEYS = [
    "name", "object_name", "table_name", "view_name", "procedure_name",
    "function_name", "trigger_name", "package_name", "job_name", "object",
]
_COMPLEXITY_KEYS = ["complexity", "complexity_score", "effort", "complexity_level"]
_SOURCE_KEYS = ["source_object", "source", "from", "from_object", "parent"]
_TARGET_KEYS = ["target_object", "target", "to", "to_object", "child"]
_RELATIONSHIP_KEYS = ["relationship_type", "relationship", "type", "dependency_type"]


def _classify(label: str) -> str | None:
    lowered = label.lower()
    for keyword, category in _CATEGORY_KEYWORDS:
        if keyword in lowered:
            return category
    return None


def _first_present(row: dict, keys: list[str]) -> str | None:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key])
    return None


def _row_to_object_ref(row: dict, category: str, source_tech: str, raw_category: str) -> LakebridgeObjectRef:
    name = _first_present(row, _NAME_KEYS) or json.dumps(row, default=str)[:80]
    return LakebridgeObjectRef(
        object_type=category,
        name=name,
        source_tech=source_tech,
        raw_category=raw_category,
        complexity=_first_present(row, _COMPLEXITY_KEYS),
    )


def _row_to_dependency_ref(row: dict, raw_category: str) -> LakebridgeDependencyRef:
    return LakebridgeDependencyRef(
        source_object=_first_present(row, _SOURCE_KEYS) or "(unknown)",
        target_object=_first_present(row, _TARGET_KEYS) or "(unknown)",
        relationship_type=_first_present(row, _RELATIONSHIP_KEYS) or "unknown",
        raw_category=raw_category,
    )


def _apply_rows(result: LakebridgeDiscoveryResult, rows: list[dict], raw_category: str, source_tech: str) -> int:
    category = _classify(raw_category)
    if category is None:
        # Try classifying by an in-row "type"/"category" field instead.
        applied = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            type_hint = _first_present(row, ["type", "object_type", "category"]) or ""
            row_category = _classify(type_hint)
            if row_category is None:
                continue
            applied += _apply_rows(result, [row], row_category, source_tech)
        return applied

    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if category == "dependency":
            result.dependencies.append(_row_to_dependency_ref(row, raw_category))
        elif category == "unsupported":
            result.unsupported_objects.append(_row_to_object_ref(row, "unsupported", source_tech, raw_category))
        else:
            bucket: list[LakebridgeObjectRef] = getattr(result, _PLURAL.get(category, category), None)
            if bucket is None:
                continue
            bucket.append(_row_to_object_ref(row, category, source_tech, raw_category))
        count += 1
    return count


_PLURAL = {
    "table": "tables", "view": "views", "stored_procedure": "stored_procedures",
    "function": "functions", "trigger": "triggers", "synonym": "synonyms",
    "schema": "schemas", "package": "packages",
}

# Verified against a real MS SQL Server Analyzer report (run 2026-07-03): the
# object inventory isn't in anything the generic keyword classifier below can
# find -- it's keyed by program/file name (Excel: "SQL Programs"."Program
# Name", JSON: top-level "inventory"[]."name"), which already carries the
# object type via source_exporter.py's own `{kind}__{schema}.{name}.ext` file
# naming convention (e.g. "table__Sales.Store.sql"). Other sheets/keys are
# usage/complexity aggregates, not object catalogs -- classifying them by
# name (e.g. Excel sheet "Functions") misreads built-in-function call counts
# as a list of user-defined functions.
_PROGRAM_NAME_HEADER = "Program Name"
_NON_INVENTORY_SHEETS = {
    "Functions", "Functions by Script", "Scripts Functions Xref",
    "SQL Script Categories", "UNKNOWN SQL Category", "SQL Special Patterns",
    "SQL Data Types", "Summary",
}


def _apply_program_inventory_rows(
    result: LakebridgeDiscoveryResult, rows: list[dict], source_tech: str, name_field: str, complexity_field: str
) -> int:
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        program_name = row.get(name_field)
        if not program_name:
            continue
        prefix, sep, rest = str(program_name).partition("__")
        if not sep:
            continue
        category = _classify(prefix.replace("_", " "))
        if category is None or category in ("dependency", "unsupported"):
            continue
        bucket = getattr(result, _PLURAL.get(category, category), None)
        if bucket is None:
            continue
        name = rest.rsplit(".", 1)[0] if "." in rest else rest
        bucket.append(LakebridgeObjectRef(
            object_type=category,
            name=name,
            source_tech=source_tech,
            raw_category=name_field,
            complexity=row.get(complexity_field) or _first_present(row, _COMPLEXITY_KEYS),
        ))
        count += 1
    return count


def _walk_json_for_lists(node: Any, path: str, result: LakebridgeDiscoveryResult, source_tech: str) -> int:
    """Recursively finds every list-of-dicts in the JSON report and applies
    it under whatever key name led to it -- handles reports shaped as a flat
    dict of category->rows, or nested under a wrapper object."""
    applied = 0
    if isinstance(node, dict):
        for key, value in node.items():
            applied += _walk_json_for_lists(value, key, result, source_tech)
    elif isinstance(node, list):
        if node and isinstance(node[0], dict):
            applied += _apply_rows(result, node, path, source_tech)
        else:
            for item in node:
                applied += _walk_json_for_lists(item, path, result, source_tech)
    return applied


def parse_json_report(report_path: Path, result: LakebridgeDiscoveryResult, source_tech: str) -> None:
    with open(report_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    applied = 0
    remaining = data
    if isinstance(data, dict) and isinstance(data.get("inventory"), list):
        applied += _apply_program_inventory_rows(
            result, data["inventory"], source_tech, name_field="name", complexity_field="complexityLevel"
        )
        remaining = {k: v for k, v in data.items() if k != "inventory"}
    applied += _walk_json_for_lists(remaining, "root", result, source_tech)
    result.raw_report_paths.append(str(report_path))
    if applied == 0:
        result.warnings.append(
            f"lakebridge report {report_path} parsed but no recognizable inventory rows were found "
            f"-- report shape may differ from what report_parser.py expects (unverified mapping)."
        )
    logger.info("Parsed JSON report %s: %d rows classified into inventory categories", report_path, applied)


def parse_excel_report(report_path: Path, result: LakebridgeDiscoveryResult, source_tech: str) -> None:
    try:
        import openpyxl
    except ImportError:
        result.warnings.append(
            f"openpyxl not installed -- cannot parse Excel report {report_path}. "
            f"pip install openpyxl, or rely on --generate-json true instead."
        )
        logger.warning("SKIP excel report %s: openpyxl not installed", report_path)
        return

    wb = openpyxl.load_workbook(report_path, read_only=True, data_only=True)
    applied = 0
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = next(rows_iter)
        except StopIteration:
            continue
        if not header:
            continue
        header = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(header)]
        rows = [dict(zip(header, row)) for row in rows_iter]
        if _PROGRAM_NAME_HEADER in header:
            applied += _apply_program_inventory_rows(
                result, rows, source_tech, name_field=_PROGRAM_NAME_HEADER, complexity_field="Complexity"
            )
            continue
        if sheet_name in _NON_INVENTORY_SHEETS:
            continue
        applied += _apply_rows(result, rows, sheet_name, source_tech)
    result.raw_report_paths.append(str(report_path))
    if applied == 0:
        result.warnings.append(
            f"lakebridge report {report_path} parsed but no recognizable inventory rows were found "
            f"-- sheet/column names may differ from what report_parser.py expects (unverified mapping)."
        )
    logger.info("Parsed Excel report %s: %d rows classified into inventory categories", report_path, applied)


def parse_invocation(entity: AnalyzeInvocationEntity, result: LakebridgeDiscoveryResult) -> None:
    """Parses one analyze invocation's report(s) into `result`, in place.
    Never raises: a malformed/missing report is recorded as a warning so it
    doesn't take down the rest of the Lakebridge Discovery run."""
    if entity.status != "success":
        if entity.status != "skipped":
            result.errors.append(f"analyze[{entity.source_tech}] did not succeed: {entity.error}")
        return

    try:
        if entity.report_json_path and Path(entity.report_json_path).exists():
            parse_json_report(Path(entity.report_json_path), result, entity.source_tech)
        elif entity.report_excel_path and Path(entity.report_excel_path).exists():
            parse_excel_report(Path(entity.report_excel_path), result, entity.source_tech)
        else:
            result.warnings.append(f"analyze[{entity.source_tech}] reported success but no report file was found on disk")
    except Exception as exc:  # noqa: BLE001 - a bad report must not crash the whole comparison
        error = f"report_parser failed for analyze[{entity.source_tech}]: {type(exc).__name__}: {exc}"
        result.errors.append(error)
        logger.error(error)
