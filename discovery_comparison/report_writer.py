"""Writes the Discovery Comparison report as JSON, CSV, and Markdown."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from discovery_comparison.logging_setup import logger
from discovery_comparison.schema import ComparisonResult


def write_json_report(result: ComparisonResult, output_dir: str, filename: str = "comparison_report.json") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, default=str)
    return out_path


def write_csv_report(result: ComparisonResult, output_dir: str, filename: str = "comparison_report.csv") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "category", "sqlglot_count", "lakebridge_count", "difference", "matched_count", "match_basis",
        ])
        writer.writeheader()
        for cat in result.categories:
            writer.writerow({
                "category": cat.category, "sqlglot_count": cat.sqlglot_count,
                "lakebridge_count": cat.lakebridge_count, "difference": cat.difference,
                "matched_count": cat.matched_count, "match_basis": cat.match_basis,
            })
    return out_path


def _engine_section(title: str, run) -> str:
    lines = [f"### {title}", "", f"- Status: **{run.status}**"]
    if run.duration_seconds is not None:
        lines.append(f"- Duration: {run.duration_seconds}s")
    if run.started_at:
        lines.append(f"- Started: {run.started_at}")
    if run.finished_at:
        lines.append(f"- Finished: {run.finished_at}")
    lines.append(f"- Errors: {run.error_count}")
    lines.append(f"- Warnings: {run.warning_count}")
    for note in run.notes:
        if note:
            lines.append(f"- Note: {note}")
    lines.append("")
    return "\n".join(lines)


def write_markdown_report(result: ComparisonResult, output_dir: str, filename: str = "comparison_report.md") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Discovery Comparison Report",
        "",
        f"Generated: {result.generated_at}",
        "",
        "Two independent Discovery engines analyzed the same source database. "
        "Neither engine consumed the other's output -- this report is the only "
        "place their results are read together.",
        "",
        "## Engine run status",
        "",
        _engine_section("SQLGlot Discovery", result.sqlglot_run),
        _engine_section("Lakebridge Discovery", result.lakebridge_run),
        "## Category comparison",
        "",
        "| Category | SQLGlot | Lakebridge | Difference | Matched (best-effort) |",
        "|---|---:|---:|---:|---:|",
    ]
    for cat in result.categories:
        lines.append(f"| {cat.category} | {cat.sqlglot_count} | {cat.lakebridge_count} | {cat.difference} | {cat.matched_count} |")
    lines.append("")

    for cat in result.categories:
        if cat.sqlglot_only_sample or cat.lakebridge_only_sample:
            lines.append(f"### {cat.category} -- name differences (best-effort match: {cat.match_basis})")
            lines.append("")
            if cat.sqlglot_only_sample:
                lines.append(f"- Only in SQLGlot ({len(cat.sqlglot_only_sample)} shown, capped): {', '.join(cat.sqlglot_only_sample)}")
            if cat.lakebridge_only_sample:
                lines.append(f"- Only in Lakebridge ({len(cat.lakebridge_only_sample)} shown, capped): {', '.join(cat.lakebridge_only_sample)}")
            lines.append("")

    if result.notes:
        lines.append("## Notes")
        lines.append("")
        for note in result.notes:
            lines.append(f"- {note}")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def write_all_reports(result: ComparisonResult, output_dir: str) -> dict[str, Path]:
    paths = {
        "json": write_json_report(result, output_dir),
        "csv": write_csv_report(result, output_dir),
        "markdown": write_markdown_report(result, output_dir),
    }
    logger.info("Wrote comparison reports: %s", ", ".join(str(p) for p in paths.values()))
    return paths
