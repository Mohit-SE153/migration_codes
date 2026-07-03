"""
Tests for the pluggable lineage-extraction engines (autovista/lineage_engines.py)
used by the sqlglot-vs-Lakebridge comparison feature. Verifies:
  - SqlglotLineageEngine wraps sql_lineage_parser.parse_lineage() faithfully
    and isolates a single bad file from the rest of its batch.
  - LakebridgeLineageEngine gracefully reports "unavailable" when the
    Databricks CLI isn't present (never raises), and correctly derives
    lineage from a (faked) successful transpile invocation.
"""
from __future__ import annotations

from unittest.mock import patch

from autovista.lineage_engines import LakebridgeLineageEngine, SqlglotLineageEngine


def test_sqlglot_engine_resolves_simple_select():
    engine = SqlglotLineageEngine()
    result = engine.run_batch({"proc_a": "SELECT * FROM dbo.Orders"}, output_dir="unused")
    assert result.available is True
    assert result.results["proc_a"].status == "resolved"
    assert result.results["proc_a"].referenced_tables == ["dbo.Orders"]


def test_sqlglot_engine_flags_dynamic_sql_as_unresolved():
    engine = SqlglotLineageEngine()
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN EXEC sp_executesql @sql END"
    result = engine.run_batch({"usp_x": sql}, output_dir="unused")
    assert result.results["usp_x"].status == "unresolved"
    assert "dynamic SQL" in result.results["usp_x"].notes


def test_sqlglot_engine_isolates_one_bad_file_from_the_rest_of_the_batch():
    engine = SqlglotLineageEngine()
    with patch("autovista.lineage_engines.parse_lineage") as mock_parse:
        def side_effect(sql_text, *args, **kwargs):
            if sql_text == "BOOM":
                raise RuntimeError("simulated parser crash")
            from autovista.sql_lineage_parser import LineageResult
            return LineageResult(referenced_tables=["dbo.Good"], referenced_procs=[], parse_status="sqlglot")

        mock_parse.side_effect = side_effect
        result = engine.run_batch({"bad": "BOOM", "good": "SELECT 1"}, output_dir="unused")

    assert result.results["bad"].status == "error"
    assert "simulated parser crash" in result.results["bad"].notes
    assert result.results["good"].status == "resolved"
    assert result.results["good"].referenced_tables == ["dbo.Good"]


def test_lakebridge_is_available_false_when_cli_missing():
    engine = LakebridgeLineageEngine(command="nonexistent-cli-xyz")
    available, reason = engine.is_available()
    assert available is False
    assert "not found on PATH" in reason


def test_lakebridge_run_batch_reports_unavailable_for_every_object_without_raising():
    engine = LakebridgeLineageEngine(command="nonexistent-cli-xyz")
    result = engine.run_batch({"a": "SELECT 1", "b": "SELECT 2"}, output_dir="unused")
    assert result.available is False
    assert result.results["a"].status == "unavailable"
    assert result.results["b"].status == "unavailable"


def test_lakebridge_is_available_false_when_probe_exits_nonzero():
    engine = LakebridgeLineageEngine(command="databricks")
    with patch("autovista.lineage_engines.shutil.which", return_value="/usr/bin/databricks"), \
         patch("autovista.lineage_engines.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "not authenticated"
        mock_run.return_value.stdout = ""
        available, reason = engine.is_available()
    assert available is False
    assert "not installed/authenticated" in reason


def test_lakebridge_run_batch_derives_lineage_from_converted_output(tmp_path):
    engine = LakebridgeLineageEngine(command="databricks", source_dialect="mssql")

    def fake_run(cmd, capture_output, text, timeout):
        # Simulate a successful transpile: write converted Databricks SQL
        # for each input file into the --output-folder the real CLI would
        # have been given.
        output_folder = cmd[cmd.index("--output-folder") + 1]
        from pathlib import Path
        Path(output_folder).mkdir(parents=True, exist_ok=True)
        (Path(output_folder) / "proc_a.sql").write_text("SELECT * FROM dbo.orders", encoding="utf-8")

        class FakeCompletedProcess:
            returncode = 0
            stderr = ""
            stdout = "ok"

        return FakeCompletedProcess()

    with patch.object(LakebridgeLineageEngine, "is_available", return_value=(True, None)), \
         patch("autovista.lineage_engines.subprocess.run", side_effect=fake_run):
        result = engine.run_batch({"proc_a": "SELECT * FROM dbo.Orders"}, output_dir=str(tmp_path))

    assert result.available is True
    assert result.results["proc_a"].status == "resolved"
    assert result.results["proc_a"].referenced_tables == ["dbo.orders"]
    assert result.results["proc_a"].generated_sql_path is not None
    assert result.engine_metadata["exit_code"] == 0


def test_lakebridge_run_batch_reports_error_when_transpile_fails(tmp_path):
    engine = LakebridgeLineageEngine(command="databricks")

    class FakeFailedProcess:
        returncode = 1
        stderr = "transpiler crashed"
        stdout = ""

    with patch.object(LakebridgeLineageEngine, "is_available", return_value=(True, None)), \
         patch("autovista.lineage_engines.subprocess.run", return_value=FakeFailedProcess()):
        result = engine.run_batch({"proc_a": "SELECT 1"}, output_dir=str(tmp_path))

    assert result.results["proc_a"].status == "error"
    assert "transpiler crashed" in result.results["proc_a"].notes
    assert result.engine_metadata["exit_code"] == 1
