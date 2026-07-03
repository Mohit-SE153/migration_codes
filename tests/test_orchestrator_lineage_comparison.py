"""
Integration tests for how the lineage-engine comparison is wired into
run_discovery(): disabled by default (zero behavior change), and when
enabled, runs alongside -- without touching -- the existing Discovery
output (discovery_manifest.json, tables.json, etc.).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from autovista.config import (
    AutovistaConfig,
    LineageComparisonConfig,
    LlmFallbackConfig,
    SqlServerConfig,
    load_config,
)
from autovista.orchestrator import run_discovery


def _fixture_config(tmp_path, *, comparison_enabled: bool, input_dir=None) -> AutovistaConfig:
    return AutovistaConfig(
        source=SqlServerConfig(host="", database="", username=None, password=None, use_integrated_auth=False),
        llm=LlmFallbackConfig(enabled=False, api_key=None, model="x", max_objects_per_run=1),
        lineage_comparison=LineageComparisonConfig(
            enabled=comparison_enabled,
            input_dir=str(input_dir) if input_dir else str(tmp_path / "unused_input"),
            lakebridge_command="nonexistent-cli-xyz",  # deterministic "unavailable" regardless of test machine
            lakebridge_source_dialect="mssql",
        ),
        run_mode="fixture",
        state_db_path=str(tmp_path / "state.sqlite3"),
        output_dir=str(tmp_path / "output"),
        dtsx_fallback_dir=None,
    )


def test_lineage_comparison_disabled_by_default_in_load_config(monkeypatch):
    for var in (
        "AUTOVISTA_LINEAGE_COMPARISON_ENABLED", "AUTOVISTA_LINEAGE_COMPARISON_INPUT_DIR",
        "AUTOVISTA_LAKEBRIDGE_COMMAND", "AUTOVISTA_LAKEBRIDGE_SOURCE_DIALECT",
    ):
        monkeypatch.delenv(var, raising=False)
    config = load_config()
    assert config.lineage_comparison.enabled is False


def test_disabled_comparison_does_not_create_lineage_comparison_output(tmp_path):
    config = _fixture_config(tmp_path, comparison_enabled=False)
    manifest = run_discovery(config)

    assert manifest.tables  # normal Discovery still ran
    assert not (tmp_path / "output" / "lineage_comparison").exists()


def test_enabled_comparison_runs_without_disturbing_discovery_output(tmp_path):
    input_dir = tmp_path / "lineage_input"
    input_dir.mkdir()
    (input_dir / "proc_a.sql").write_text("SELECT * FROM dbo.Orders", encoding="utf-8")

    config = _fixture_config(tmp_path, comparison_enabled=True, input_dir=input_dir)
    manifest = run_discovery(config)

    output_dir = tmp_path / "output"
    # Core Discovery output: unaffected, still exactly what fixture mode produces.
    assert (output_dir / "discovery_manifest.json").exists()
    assert (output_dir / "tables.json").exists()
    assert manifest.tables

    # New, separate lineage comparison output.
    comparison_dir = output_dir / "lineage_comparison"
    assert (comparison_dir / "sqlglot" / "proc_a.json").exists()
    assert (comparison_dir / "lakebridge" / "proc_a.json").exists()
    assert (comparison_dir / "comparison" / "comparison_report.json").exists()

    import json
    lakebridge_result = json.loads((comparison_dir / "lakebridge" / "proc_a.json").read_text(encoding="utf-8"))
    assert lakebridge_result["status"] == "unavailable"

    sqlglot_result = json.loads((comparison_dir / "sqlglot" / "proc_a.json").read_text(encoding="utf-8"))
    assert sqlglot_result["referenced_tables"] == ["dbo.Orders"]


def test_comparison_crash_does_not_prevent_discovery_output_or_raise(tmp_path):
    config = _fixture_config(tmp_path, comparison_enabled=True)

    with patch("autovista.orchestrator.run_lineage_engine_comparison", side_effect=RuntimeError("boom")):
        manifest = run_discovery(config)  # must not raise

    assert manifest.tables
    assert (tmp_path / "output" / "discovery_manifest.json").exists()
