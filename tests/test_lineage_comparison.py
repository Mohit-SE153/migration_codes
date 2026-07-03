"""
Tests for lineage_comparison.py's orchestration: running multiple engines
over the same input folder, writing per-engine output + JSON/CSV/Markdown
comparison reports, and isolating one engine's crash from the other's
results and from the report itself.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field

from autovista.lineage_comparison import run_lineage_engine_comparison
from autovista.lineage_engines import EngineBatchResult, EngineLineageResult


@dataclass
class StubEngine:
    """A minimal LineageEngine stub for testing the comparison
    orchestration in isolation from any real parsing logic."""

    name: str
    canned_results: dict[str, EngineLineageResult] = field(default_factory=dict)
    should_raise: bool = False

    def is_available(self) -> tuple[bool, str | None]:
        return True, None

    def run_batch(self, sql_files: dict[str, str], output_dir: str) -> EngineBatchResult:
        if self.should_raise:
            raise RuntimeError(f"{self.name} exploded")
        return EngineBatchResult(
            engine_name=self.name, available=True, unavailable_reason=None,
            duration_ms=1.23, results=self.canned_results, engine_metadata={"stub": True},
        )


def _write_sql_files(input_dir, files: dict[str, str]):
    for name, text in files.items():
        (input_dir / f"{name}.sql").write_text(text, encoding="utf-8")


def test_writes_per_engine_output_files_mirrored_across_engines(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    _write_sql_files(input_dir, {"proc_a": "SELECT * FROM dbo.Orders"})

    engine_a = StubEngine(name="engine_a", canned_results={
        "proc_a": EngineLineageResult(object_name="proc_a", referenced_tables=["dbo.Orders"], status="resolved"),
    })
    engine_b = StubEngine(name="engine_b", canned_results={
        "proc_a": EngineLineageResult(object_name="proc_a", referenced_tables=["dbo.orders"], status="resolved"),
    })

    output_dir = tmp_path / "output"
    run_lineage_engine_comparison(str(input_dir), str(output_dir), [engine_a, engine_b])

    assert (output_dir / "engine_a" / "proc_a.json").exists()
    assert (output_dir / "engine_b" / "proc_a.json").exists()
    assert (output_dir / "engine_a" / "_engine_summary.json").exists()
    assert (output_dir / "comparison" / "comparison_report.json").exists()
    assert (output_dir / "comparison" / "comparison_report.csv").exists()
    assert (output_dir / "comparison" / "comparison_report.md").exists()


def test_comparison_report_flags_table_reference_disagreement(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    _write_sql_files(input_dir, {"proc_a": "SELECT * FROM dbo.Orders"})

    engine_a = StubEngine(name="engine_a", canned_results={
        "proc_a": EngineLineageResult(object_name="proc_a", referenced_tables=["dbo.Orders"], status="resolved"),
    })
    engine_b = StubEngine(name="engine_b", canned_results={
        "proc_a": EngineLineageResult(object_name="proc_a", referenced_tables=["dbo.SomethingElse"], status="resolved"),
    })

    output_dir = tmp_path / "output"
    report = run_lineage_engine_comparison(str(input_dir), str(output_dir), [engine_a, engine_b])

    row = report["objects"][0]
    assert row["tables_agree"] is False
    assert row["tables_only_in_engine_a"] == ["dbo.Orders"]
    assert row["tables_only_in_engine_b"] == ["dbo.SomethingElse"]


def test_one_engine_crashing_does_not_prevent_the_others_results_or_the_report(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    _write_sql_files(input_dir, {"proc_a": "SELECT * FROM dbo.Orders"})

    healthy = StubEngine(name="healthy", canned_results={
        "proc_a": EngineLineageResult(object_name="proc_a", referenced_tables=["dbo.Orders"], status="resolved"),
    })
    exploding = StubEngine(name="exploding", should_raise=True)

    output_dir = tmp_path / "output"
    report = run_lineage_engine_comparison(str(input_dir), str(output_dir), [healthy, exploding])

    healthy_stats = next(e for e in report["engines"] if e["engine_name"] == "healthy")
    exploding_stats = next(e for e in report["engines"] if e["engine_name"] == "exploding")
    assert healthy_stats["available"] is True
    assert healthy_stats["resolved"] == 1
    assert exploding_stats["available"] is False
    assert "exploded" in exploding_stats["unavailable_reason"]

    # The comparison report itself was still produced -- one engine's
    # crash doesn't take down the whole comparison run.
    assert (output_dir / "comparison" / "comparison_report.json").exists()
    assert (output_dir / "healthy" / "proc_a.json").exists()


def test_engine_stats_compute_success_rate_and_status_breakdown(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    _write_sql_files(input_dir, {"a": "SELECT 1", "b": "SELECT 2", "c": "SELECT 3"})

    engine = StubEngine(name="e", canned_results={
        "a": EngineLineageResult(object_name="a", status="resolved"),
        "b": EngineLineageResult(object_name="b", status="unresolved"),
        "c": EngineLineageResult(object_name="c", status="error"),
    })

    output_dir = tmp_path / "output"
    report = run_lineage_engine_comparison(str(input_dir), str(output_dir), [engine])

    stats = report["engines"][0]
    assert stats["resolved"] == 1
    assert stats["unresolved"] == 1
    assert stats["failed"] == 1
    assert stats["conversion_success_rate_pct"] == round(1 / 3 * 100.0, 2)


def test_csv_and_markdown_reports_are_well_formed(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    _write_sql_files(input_dir, {"proc_a": "SELECT 1"})

    engine = StubEngine(name="e", canned_results={
        "proc_a": EngineLineageResult(object_name="proc_a", referenced_tables=["dbo.X"], status="resolved"),
    })
    output_dir = tmp_path / "output"
    run_lineage_engine_comparison(str(input_dir), str(output_dir), [engine])

    with open(output_dir / "comparison" / "comparison_report.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["object_name"] == "proc_a"
    assert rows[0]["e_status"] == "resolved"

    md_text = (output_dir / "comparison" / "comparison_report.md").read_text(encoding="utf-8")
    assert "# Lineage Engine Comparison Report" in md_text
    assert "proc_a" in md_text


def test_empty_input_dir_produces_a_valid_zero_object_report(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    output_dir = tmp_path / "output"

    report = run_lineage_engine_comparison(str(input_dir), str(output_dir), [StubEngine(name="e")])
    assert report["object_count"] == 0
    assert report["objects"] == []
