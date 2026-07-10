"""
Writes the LLM Assessment output contract. Fully self-contained
independent copy (not imported from assessment/output_writer.py) -- see
schema.py's module docstring for why. Default filenames are this
package's own (llm_assessment_manifest.json, etc.) rather than needing
filename overrides from the caller.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from llm_assessment.logging_setup import logger
from llm_assessment.schema import AssessmentManifest

ENTITY_OUTPUT_FILES = {
    "object_complexity": "object_complexity.json",
    "risk_register": "risk_register.json",
    "migration_waves": "migration_waves.json",
    "data_readiness": "data_readiness.json",
    "security_notes": "security_notes.json",
    "infra_sizing": "infra_sizing.json",
    "summary": "assessment_summary.json",
}


def write_entity_outputs(manifest: AssessmentManifest, output_dir: str) -> dict[str, Path]:
    manifest_dict = manifest.to_dict()
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}
    for field_name, filename in ENTITY_OUTPUT_FILES.items():
        out_path = out_dir / filename
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(manifest_dict[field_name], f, indent=2, default=str)
        paths[field_name] = out_path

    logger.info("Wrote %d per-category output files to %s", len(paths), out_dir)
    return paths


def write_manifest_json(manifest: AssessmentManifest, output_dir: str, filename: str = "llm_assessment_manifest.json") -> Path:
    write_entity_outputs(manifest, output_dir)
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, indent=2, default=str)
    return out_path


def write_csv_rollup(manifest: AssessmentManifest, output_dir: str, filename: str = "llm_assessment_rollup.csv") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    summary = manifest.summary
    if summary is not None:
        rows.append({"category": "total_objects_scored", "key": "(all)", "count": summary.total_objects_scored})
        rows.append({"category": "total_estimated_hours", "key": "(all)", "count": summary.total_estimated_hours})
        for tier, count in summary.complexity_tier_counts.items():
            rows.append({"category": "complexity_tier", "key": tier, "count": count})
        for severity, count in summary.risk_counts_by_severity.items():
            rows.append({"category": "risk_severity", "key": severity, "count": count})
        for risk_category, count in summary.risk_counts_by_category.items():
            rows.append({"category": "risk_category", "key": risk_category, "count": count})
        rows.append({"category": "migration_waves", "key": "(all)", "count": summary.total_migration_waves})
        rows.append({"category": "waves_with_circular_dependencies", "key": "(all)", "count": summary.waves_with_circular_dependencies})

    rows.append({"category": "data_readiness_findings", "key": "(all)", "count": len(manifest.data_readiness)})
    rows.append({"category": "security_notes", "key": "(all)", "count": len(manifest.security_notes)})
    rows.append({"category": "infra_sizing_recommendations", "key": "(all)", "count": len(manifest.infra_sizing)})

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["category", "key", "count"])
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def write_risk_register_csv(manifest: AssessmentManifest, output_dir: str, filename: str = "risk_register.csv") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["object_type", "name", "category", "severity", "description", "remediation", "needs_human_review"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in manifest.risk_register:
            writer.writerow({
                "object_type": r.object_type, "name": r.name, "category": r.category,
                "severity": r.severity, "description": r.description,
                "remediation": r.remediation or "", "needs_human_review": r.needs_human_review,
            })
    return out_path


def write_object_complexity_csv(manifest: AssessmentManifest, output_dir: str, filename: str = "object_complexity.csv") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["object_type", "name", "complexity_tier", "complexity_score", "estimated_hours", "fan_in", "fan_out", "scoring_reasons"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for oc in sorted(manifest.object_complexity, key=lambda o: o.complexity_score, reverse=True):
            writer.writerow({
                "object_type": oc.object_type, "name": oc.name, "complexity_tier": oc.complexity_tier,
                "complexity_score": oc.complexity_score, "estimated_hours": oc.estimated_hours,
                "fan_in": oc.fan_in, "fan_out": oc.fan_out, "scoring_reasons": "; ".join(oc.scoring_reasons),
            })
    return out_path


def write_migration_waves_csv(manifest: AssessmentManifest, output_dir: str, filename: str = "migration_waves.csv") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["wave_number", "object_count", "estimated_hours", "has_circular_dependency", "rationale", "objects"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for w in manifest.migration_waves:
            writer.writerow({
                "wave_number": w.wave_number, "object_count": w.object_count,
                "estimated_hours": w.estimated_hours, "has_circular_dependency": w.has_circular_dependency,
                "rationale": w.rationale, "objects": "; ".join(w.objects),
            })
    return out_path


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_MD_CELL_MAX_LEN = 300


def _md_cell(value) -> str:
    text = _ANSI_ESCAPE.sub("", str(value))
    text = " ".join(text.split())
    text = text.replace("|", "\\|")
    if len(text) > _MD_CELL_MAX_LEN:
        text = text[:_MD_CELL_MAX_LEN - 3] + "..."
    return text


def _md_table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(cell) for cell in row) + " |")
    return "\n".join(lines)


def write_markdown_report(manifest: AssessmentManifest, output_dir: str, filename: str = "llm_assessment_report.md") -> Path:
    out_path = Path(output_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary = manifest.summary

    lines: list[str] = [
        f"# LLM Migration Assessment Report -- {manifest.database}",
        "",
        f"Generated: {manifest.generated_at}",
        f"Source Discovery manifest: `{manifest.source_manifest_path}` (sqlglot engine only)",
        "",
        "## Executive summary",
        "",
    ]

    if summary is not None:
        lines += [
            f"- **{summary.total_objects_scored}** objects scored (tables, views, stored procedures, functions, triggers).",
            f"- **{summary.total_estimated_hours} hours** total estimated remediation/migration effort "
            f"(rubric: {summary.effort_rubric_hours} hours per tier -- an assumed, editable planning heuristic, not a measured fact).",
            f"- **{sum(summary.risk_counts_by_severity.values())}** risk-register findings "
            f"({', '.join(f'{v} {k}' for k, v in sorted(summary.risk_counts_by_severity.items(), key=lambda kv: kv[0]))}).",
            f"- **{summary.total_migration_waves}** migration waves identified"
            + (f", {summary.waves_with_circular_dependencies} of which contain a circular dependency."
               if summary.waves_with_circular_dependencies else "."),
            "",
            "### Complexity tier breakdown",
            "",
            _md_table(["Tier", "Object count"], sorted(summary.complexity_tier_counts.items())),
            "",
            "### Top riskiest objects (by complexity score)",
            "",
            "\n".join(f"{i + 1}. {name}" for i, name in enumerate(summary.top_riskiest_objects)) or "(none)",
            "",
        ]

    lines += [
        "## Risk register",
        "",
        _md_table(
            ["Object type", "Name", "Category", "Severity", "Description"],
            [[r.object_type, r.name, r.category, r.severity, r.description] for r in manifest.risk_register],
        ) if manifest.risk_register else "No risk-register findings.",
        "",
        "## Migration wave plan",
        "",
        _md_table(
            ["Wave", "Objects", "Est. hours", "Circular?", "Rationale"],
            [[w.wave_number, w.object_count, w.estimated_hours, "yes" if w.has_circular_dependency else "no", w.rationale]
             for w in manifest.migration_waves],
        ) if manifest.migration_waves else "No migration waves (no scoped objects found).",
        "",
        "## Data readiness findings",
        "",
        _md_table(
            ["Category", "Count", "Severity", "Description", "Recommendation"],
            [[f.category, f.count, f.severity, f.description, f.recommendation] for f in manifest.data_readiness],
        ) if manifest.data_readiness else "No data-readiness findings.",
        "",
        "## Security / permissions migration notes",
        "",
        _md_table(
            ["Category", "Count", "Severity", "Description", "Recommendation"],
            [[n.category, n.count, n.severity, n.description, n.recommendation] for n in manifest.security_notes],
        ) if manifest.security_notes else "No security notes.",
        "",
        "## Databricks infrastructure sizing recommendations",
        "",
        "Deterministic, threshold-based recommendations from Discovery's database/table size metadata -- "
        "grounded in Databricks' own published SQL warehouse t-shirt sizes and partitioning/Liquid Clustering "
        "guidance (see infra_sizing.py), not an LLM judgment call. A capacity-planning starting point, not a "
        "committed infra spec.",
        "",
        _md_table(
            ["Category", "Current metric", "Recommendation", "Rationale"],
            [[r.category, r.current_metric, r.recommendation, r.rationale] for r in manifest.infra_sizing],
        ) if manifest.infra_sizing else "No infra-sizing recommendations.",
        "",
    ]

    if manifest.warnings:
        lines += ["## Warnings", "", "\n".join(f"- {w}" for w in manifest.warnings), ""]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path
