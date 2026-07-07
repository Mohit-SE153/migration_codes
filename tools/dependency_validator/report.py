"""
Turns a list of classify.ClassifiedDependency into the five report files
plus the console summary, and checks for regressions against the previous
run's coverage_summary.json (if one exists) before overwriting it.

Category grouping for coverage_by_category.csv is by (source label, target
label) -- e.g. "Procedure -> Table" -- with a few named special cases
(Foreign Keys, Synonyms) matching the user's own example rows rather than
the generic "Table -> Table" / "Synonym -> Table" shape.
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from tools.dependency_validator.classify import (
    KNOWN_UNSUPPORTED,
    MATCHED,
    MISSING,
    OUT_OF_SCOPE,
    ClassifiedDependency,
)

_SOURCE_LABELS = {
    "stored_procedure": "Procedure",
    "view": "View",
    "function": "Function",
    "trigger": "Trigger",
    "table": "Table",
    "synonym": "Synonym",
}
_TARGET_LABELS = {
    "table": "Table",
    "view": "View",
    "stored_procedure": "Procedure",
    "function": "Function",
    "sequence": "Sequence",
    "user_defined_type": "User Defined Type",
    "xml_schema_collection": "XML Schema Collection",
    "synonym": "Synonym",
}


def category_label(dep: ClassifiedDependency, referencing_type_raw: str | None = None) -> str:
    if dep.source_type == "table" and dep.target_type == "table" and dep.relationship_type == "foreign_key":
        return "Foreign Keys"
    if dep.source_type == "synonym" and dep.relationship_type == "references":
        return "Synonyms"
    if referencing_type_raw == "CHECK_CONSTRAINT":
        source_label = "Check Constraint"
    elif referencing_type_raw == "DEFAULT_CONSTRAINT":
        source_label = "Default Constraint"
    else:
        source_label = _SOURCE_LABELS.get(dep.source_type, dep.source_type.replace("_", " ").title())
    target_label = _TARGET_LABELS.get(dep.target_type, dep.target_type.replace("_", " ").title())
    return f"{source_label} -> {target_label}"


def _sqlglot_shape_counts(dependencies: list[dict]) -> Counter:
    """Counts dependencies.json edges by (source_type, target_type,
    relationship_type) -- used for coverage_by_category.csv's "SQLGlot
    Count" column, computed directly from the SQLGlot side rather than
    inferred from ground-truth matches, so it reflects what SQLGlot itself
    actually emitted for that shape."""
    return Counter((d["source_type"], d["target_type"], d["relationship_type"]) for d in dependencies)


def build_coverage_report(
    classified: list[tuple[ClassifiedDependency, str | None]],  # (dependency, raw referencing_type)
    sqlglot_dependencies: list[dict],
) -> dict:
    """Returns the full in-memory report structure -- report.py's writers
    (write_reports) turn this into files; kept separate so tests can
    assert on the structure without touching the filesystem."""
    migration_relevant = [d for d, _ in classified if d.category in (MATCHED, MISSING, KNOWN_UNSUPPORTED)]
    matched = [d for d, _ in classified if d.category == MATCHED]
    missing = [d for d, _ in classified if d.category == MISSING]
    known_unsupported = [d for d, _ in classified if d.category == KNOWN_UNSUPPORTED]
    out_of_scope = [d for d, _ in classified if d.category == OUT_OF_SCOPE]
    representation_differences = [d for d in matched if d.representation_difference]

    coverage_denominator = len(matched) + len(missing)
    coverage_pct = round(len(matched) / coverage_denominator * 100, 2) if coverage_denominator else 100.0

    by_category: dict[str, dict] = defaultdict(lambda: {"sql_server_count": 0, "sqlglot_count": 0, "matched": 0, "missing": 0})
    for dep, raw_type in classified:
        if dep.category not in (MATCHED, MISSING, KNOWN_UNSUPPORTED):
            continue
        label = category_label(dep, raw_type)
        by_category[label]["sql_server_count"] += 1
        if dep.category == MATCHED:
            by_category[label]["matched"] += 1
        elif dep.category == MISSING:
            by_category[label]["missing"] += 1

    # A single category label can span more than one (source_type,
    # target_type, relationship_type) shape -- e.g. "Trigger -> Table"
    # covers both relationship_type="reads" and "fires_on" -- so the
    # SQLGlot-side count must be summed over every distinct shape seen for
    # that label, not overwritten by whichever shape was processed last.
    sqlglot_shape_counts = _sqlglot_shape_counts(sqlglot_dependencies)
    label_shapes: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for dep, raw_type in classified:
        if dep.category not in (MATCHED, MISSING, KNOWN_UNSUPPORTED):
            continue
        label_shapes[category_label(dep, raw_type)].add((dep.source_type, dep.target_type, dep.relationship_type))
    for label, shapes in label_shapes.items():
        by_category[label]["sqlglot_count"] = sum(sqlglot_shape_counts.get(shape, 0) for shape in shapes)

    missing_categories = sorted({category_label(d, rt) for d, rt in classified if d.category == MISSING})

    return {
        "migration_relevant": {
            "matched": len(matched),
            "missing": len(missing),
            "coverage_pct": coverage_pct,
            "known_unsupported": len(known_unsupported),
        },
        "out_of_scope": len(out_of_scope),
        "representation_differences": len(representation_differences),
        "missing_categories": missing_categories,
        "by_category": dict(by_category),
        "totals": {
            "sql_server_dependencies_seen": len(classified),
            "migration_relevant_total": len(migration_relevant),
        },
    }


def _ensure_output_dir(out: Path) -> None:
    """mkdir(parents=True, exist_ok=True), tolerant of a Windows quirk
    root-caused on this project's own dev machine: a filesystem
    minifilter (security software hooking attribute-query I/O for
    on-access scanning) can make CreateDirectory report ERROR_ALREADY_EXISTS
    (183) for a path that GetFileAttributes-based checks (os.path.exists(),
    Path.is_dir()) simultaneously report as absent, even though the
    directory is genuinely there and fully usable (confirmed via
    os.scandir(), which isn't affected the same way -- see the size
    verification later in write_reports() for the same pattern). exist_ok
    alone doesn't help since the error is really raised, not a stale
    check, so FileExistsError here is treated as "already there" rather
    than retried -- if something is genuinely wrong (e.g. a real file
    occupies this path), the first file write inside it will fail with a
    clear, honest error instead."""
    try:
        out.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        pass


def write_reports(
    classified: list[tuple[ClassifiedDependency, str | None]],
    sqlglot_dependencies: list[dict],
    output_dir: str,
) -> dict:
    # Resolved to an absolute path up front. output_dir was previously used
    # as-is -- a bare relative string from the CLI -- which wrote reports
    # relative to whatever the process's current working directory
    # happened to be at invocation time, not necessarily the repo root.
    # Resolving here means a run's reports always land in one predictable,
    # absolute location regardless of where the command was launched from
    # (the CLI entrypoint additionally anchors its own default to the repo
    # root -- see tools/validate_sqlglot_dependencies.py -- so this
    # .resolve() is a second, independent safeguard, not the only one).
    out = Path(output_dir).resolve()
    print("DEBUG 1:", out)
    _ensure_output_dir(out)
    print("DEBUG 2:", out.exists(), out.is_dir())

    report = build_coverage_report(classified, sqlglot_dependencies)

    previous_summary = None
    summary_path = out / "coverage_summary.json"
    # Read-then-recover rather than gating on summary_path.exists() first --
    # .exists() is the same unreliable check documented below (a security
    # tool's filesystem filter can make a file that's genuinely there, per
    # directory listing, report as absent to an attribute-query check).
    # Attempting the read directly and treating "not found" as "no previous
    # run" via the exception is the actually-correct way to handle it here.
    try:
        previous_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        previous_summary = None

    written_paths: list[Path] = []

    def _write_text(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        written_paths.append(path)

    def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        written_paths.append(path)

    _write_text(summary_path, json.dumps(report, indent=2))
    print("DEBUG 3:", summary_path)
    print("DEBUG 4:", summary_path.exists())

    missing = [d for d, _ in classified if d.category == MISSING]
    _write_csv(
        out / "missing_dependencies.csv",
        ["source_object", "source_type", "target_object", "target_type", "relationship_type"],
        [[d.source_object, d.source_type, d.target_object, d.target_type, d.relationship_type] for d in missing],
    )

    out_of_scope = [d for d, _ in classified if d.category == OUT_OF_SCOPE]
    _write_csv(
        out / "out_of_scope.csv",
        ["source_object", "source_type", "target_object", "target_type", "reason"],
        [[d.source_object, d.source_type, d.target_object, d.target_type, d.reason] for d in out_of_scope],
    )

    representation_differences = [d for d, _ in classified if d.category == MATCHED and d.representation_difference]
    _write_csv(
        out / "representation_differences.csv",
        ["source_object", "source_type", "target_object", "target_type", "relationship_type"],
        [
            [d.source_object, d.source_type, d.target_object, d.target_type, d.relationship_type]
            for d in representation_differences
        ],
    )

    category_rows = []
    for label, counts in sorted(report["by_category"].items()):
        denom = counts["matched"] + counts["missing"]
        pct = round(counts["matched"] / denom * 100, 2) if denom else 100.0
        category_rows.append([label, counts["sql_server_count"], counts["sqlglot_count"], counts["matched"], pct])
    _write_csv(
        out / "coverage_by_category.csv",
        ["Category", "SQL Server Count", "SQLGlot Count", "Matched", "Coverage %"],
        category_rows,
    )

    # Verify every report actually persisted -- a successful write() call is
    # not proof a file survives on disk. Root-caused on this project's own
    # dev machine: Path.exists()/os.path.exists()/Path.stat() can report a
    # just-written file as absent (GetFileAttributes-style query) while
    # os.scandir() on the same directory correctly lists it and its real
    # size -- a known Windows filesystem-minifilter interaction (security
    # software hooking the attribute-query path for on-access scanning
    # without equally intercepting directory enumeration). Verifying via
    # os.scandir() rather than Path.exists()/Path.stat() is therefore the
    # actually-reliable check here, not a slower version of the same one.
    def _scan_sizes(directory: Path) -> dict[str, int]:
        try:
            with os.scandir(directory) as it:
                return {entry.name: entry.stat().st_size for entry in it}
        except OSError:
            return {}

    sizes_by_name = _scan_sizes(out)
    missing_on_disk = [str(p) for p in written_paths if sizes_by_name.get(p.name, 0) <= 0]
    if missing_on_disk:
        raise RuntimeError(
            "Dependency validator reports were written but did not persist to disk "
            f"(verified via directory listing of '{out}'). Files that did not survive: "
            + ", ".join(missing_on_disk)
        )

    report["_previous_summary"] = previous_summary
    report["_report_dir"] = str(out)
    report["_report_files"] = [str(p) for p in written_paths]
    return report


def _regression_lines(report: dict) -> list[str]:
    previous = report.get("_previous_summary")
    if not previous:
        return ["No regressions detected."]

    lines: list[str] = []
    prev_mr = previous.get("migration_relevant", {})
    curr_mr = report["migration_relevant"]
    if curr_mr["matched"] < prev_mr.get("matched", 0):
        lines.append(f"REGRESSION: overall matched count dropped {prev_mr.get('matched', 0)} -> {curr_mr['matched']}")
    if curr_mr["coverage_pct"] < prev_mr.get("coverage_pct", 0):
        lines.append(f"REGRESSION: overall coverage dropped {prev_mr.get('coverage_pct', 0)}% -> {curr_mr['coverage_pct']}%")

    prev_by_category = previous.get("by_category", {})
    for label, counts in report["by_category"].items():
        prev_counts = prev_by_category.get(label)
        if prev_counts and counts["matched"] < prev_counts.get("matched", 0):
            lines.append(f"REGRESSION: '{label}' matched count dropped {prev_counts.get('matched', 0)} -> {counts['matched']}")

    return lines or ["No regressions detected."]


def print_console_summary(report: dict) -> None:
    mr = report["migration_relevant"]
    print()
    print("SQLGlot Dependency Coverage")
    print()
    print("Migration Relevant")
    print()
    print("Matched:")
    print(mr["matched"])
    print()
    print("Missing:")
    print(mr["missing"])
    print()
    print("Coverage:")
    print(f"{mr['coverage_pct']}%")
    print()
    print("Known Unsupported:")
    print(mr["known_unsupported"])
    print()
    print("Out of Scope:")
    print(report["out_of_scope"])
    print()
    print("Representation Differences:")
    print(report["representation_differences"])
    print()
    print("---")
    print()
    if report["missing_categories"]:
        print("Missing Categories")
        print()
        for label in report["missing_categories"]:
            print(label)
    else:
        print("Missing Categories: none")
    print()
    print("---")
    print()
    for line in _regression_lines(report):
        print(line)
    print()
    print("---")
    print()
    print(f"Reports written to: {report.get('_report_dir', '(unknown)')}")
    for path in report.get("_report_files", []):
        print(f"  {path}")
