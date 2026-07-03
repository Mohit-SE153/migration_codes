"""
Runs every registered LineageEngine (see lineage_engines.py) over the same
folder of raw .sql files and writes:

  output/lineage_comparison/<engine_name>/<object_name>.json  -- per-engine,
      per-object results (mirrored structure across engines, so sqlglot's
      and Lakebridge's outputs stay organized identically to each other)
  output/lineage_comparison/comparison/comparison_report.json
  output/lineage_comparison/comparison/comparison_report.csv
  output/lineage_comparison/comparison/comparison_report.md

This is entirely separate from the core Discovery pipeline's output
(discovery_manifest.json, tables.json, stored_procedures.json, etc. are
never read or written by anything here) -- see orchestrator.py's gated,
isolated call into run_lineage_engine_comparison for how the two are kept
independent.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from autovista.lineage_engines import EngineBatchResult, EngineLineageResult, LineageEngine
from autovista.logging_setup import logger


def _load_sql_files(input_dir: str) -> dict[str, str]:
    files = {}
    for path in sorted(Path(input_dir).glob("*.sql")):
        files[path.stem] = path.read_text(encoding="utf-8")
    return files


def _write_engine_outputs(engine_result: EngineBatchResult, output_dir: str) -> Path:
    engine_dir = Path(output_dir) / engine_result.engine_name
    engine_dir.mkdir(parents=True, exist_ok=True)
    for object_name, result in engine_result.results.items():
        with open(engine_dir / f"{object_name}.json", "w", encoding="utf-8") as f:
            json.dump(asdict(result), f, indent=2, default=str)
    with open(engine_dir / "_engine_summary.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "engine_name": engine_result.engine_name,
                "available": engine_result.available,
                "unavailable_reason": engine_result.unavailable_reason,
                "duration_ms": round(engine_result.duration_ms, 2),
                "engine_metadata": engine_result.engine_metadata,
            },
            f, indent=2, default=str,
        )
    return engine_dir


def _engine_stats(engine_result: EngineBatchResult) -> dict:
    total = len(engine_result.results)
    by_status = {"resolved": 0, "unresolved": 0, "error": 0, "unavailable": 0}
    for r in engine_result.results.values():
        by_status[r.status] = by_status.get(r.status, 0) + 1
    success_rate = round(by_status["resolved"] / total * 100.0, 2) if total else None
    return {
        "engine_name": engine_result.engine_name,
        "available": engine_result.available,
        "unavailable_reason": engine_result.unavailable_reason,
        "duration_ms": round(engine_result.duration_ms, 2),
        "total_objects": total,
        "resolved": by_status["resolved"],
        "unresolved": by_status["unresolved"],
        "failed": by_status["error"],
        "conversion_success_rate_pct": success_rate,
        "engine_metadata": engine_result.engine_metadata,
    }


def _per_object_comparison(object_names: list[str], engine_results: dict[str, EngineBatchResult]) -> list[dict]:
    rows = []
    for object_name in object_names:
        row = {"object_name": object_name}
        table_sets = {}
        for engine_name, batch in engine_results.items():
            result = batch.results.get(object_name)
            row[f"{engine_name}_status"] = result.status if result else "missing"
            row[f"{engine_name}_referenced_tables"] = result.referenced_tables if result else []
            row[f"{engine_name}_referenced_procs"] = result.referenced_procs if result else []
            row[f"{engine_name}_notes"] = result.notes if result else None
            table_sets[engine_name] = set(result.referenced_tables) if result else set()

        engine_names = list(engine_results.keys())
        if len(engine_names) >= 2:
            a, b = engine_names[0], engine_names[1]
            row["tables_agree"] = table_sets.get(a, set()) == table_sets.get(b, set())
            row["tables_only_in_" + a] = sorted(table_sets.get(a, set()) - table_sets.get(b, set()))
            row["tables_only_in_" + b] = sorted(table_sets.get(b, set()) - table_sets.get(a, set()))
        rows.append(row)
    return rows


def run_lineage_engine_comparison(
    input_dir: str, output_dir: str, engines: list[LineageEngine],
) -> dict:
    """Entry point called from orchestrator.py. Returns the assembled
    comparison report dict (also written to disk). Each engine's run_batch
    is called independently and wrapped here too (belt-and-suspenders on
    top of run_batch's own internal error isolation) -- one engine raising
    an unexpected exception must never prevent the other engine's results
    or the comparison report from being produced."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    sql_files = _load_sql_files(input_dir)

    logger.info(
        "=== Lineage engine comparison starting: %d input file(s) from %s ===",
        len(sql_files), input_dir,
    )

    engine_results: dict[str, EngineBatchResult] = {}
    for engine in engines:
        logger.info("Running lineage engine: %s", engine.name)
        try:
            result = engine.run_batch(sql_files, output_dir)
        except Exception as exc:  # noqa: BLE001 - one engine's crash must not affect the other
            logger.error("Lineage engine %s crashed: %s: %s", engine.name, type(exc).__name__, exc)
            reason = f"{type(exc).__name__}: {exc}"
            result = EngineBatchResult(
                engine_name=engine.name, available=False, unavailable_reason=reason,
                duration_ms=0.0,
                results={
                    name: EngineLineageResult(object_name=name, status="error", notes=reason)
                    for name in sql_files
                },
            )
        engine_results[engine.name] = result
        status_word = "OK" if result.available else "UNAVAILABLE"
        logger.info(
            "Lineage engine %-10s %s (%.1fms) -- %s",
            engine.name, status_word, result.duration_ms,
            result.unavailable_reason or f"{len(result.results)} object(s) processed",
        )
        engine_dir = _write_engine_outputs(result, output_dir)
        logger.info("Wrote %s engine output to %s", engine.name, engine_dir)

    comparison_dir = Path(output_dir) / "comparison"
    comparison_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "input_dir": input_dir,
        "object_count": len(sql_files),
        "engines": [_engine_stats(r) for r in engine_results.values()],
        "objects": _per_object_comparison(sorted(sql_files.keys()), engine_results),
    }

    json_path = comparison_dir / "comparison_report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    csv_path = comparison_dir / "comparison_report.csv"
    _write_csv_report(report, csv_path)

    md_path = comparison_dir / "comparison_report.md"
    _write_markdown_report(report, md_path)

    logger.info(
        "=== Lineage engine comparison finished: report=%s (json/csv/md) ===", comparison_dir,
    )
    return report


def _write_csv_report(report: dict, csv_path: Path) -> None:
    fieldnames = ["object_name"]
    for engine in report["engines"]:
        fieldnames += [f"{engine['engine_name']}_status", f"{engine['engine_name']}_referenced_tables"]
    fieldnames.append("tables_agree")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in report["objects"]:
            flat_row = dict(row)
            for engine in report["engines"]:
                key = f"{engine['engine_name']}_referenced_tables"
                if key in flat_row:
                    flat_row[key] = "; ".join(flat_row[key])
            writer.writerow(flat_row)


def _write_markdown_report(report: dict, md_path: Path) -> None:
    lines = ["# Lineage Engine Comparison Report", ""]
    lines.append(f"Input directory: `{report['input_dir']}`  ")
    lines.append(f"Objects compared: **{report['object_count']}**")
    lines.append("")
    lines.append("## Engine Summary")
    lines.append("")
    lines.append("| Engine | Available | Resolved | Unresolved | Failed | Success Rate | Duration (ms) |")
    lines.append("|---|---|---|---|---|---|---|")
    for engine in report["engines"]:
        lines.append(
            f"| {engine['engine_name']} | {engine['available']} | {engine['resolved']} | "
            f"{engine['unresolved']} | {engine['failed']} | "
            f"{engine['conversion_success_rate_pct']} | {engine['duration_ms']} |"
        )
        if not engine["available"]:
            lines.append(f"  - *unavailable: {engine['unavailable_reason']}*")

    lines.append("")
    lines.append("## Per-Object Lineage Agreement")
    lines.append("")
    lines.append("| Object | Tables Agree | Notes |")
    lines.append("|---|---|---|")
    for row in report["objects"]:
        agree = row.get("tables_agree", "n/a")
        note_bits = []
        for key, value in row.items():
            if key.startswith("tables_only_in_") and value:
                note_bits.append(f"{key}={value}")
        lines.append(f"| {row['object_name']} | {agree} | {'; '.join(note_bits) or '-'} |")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
