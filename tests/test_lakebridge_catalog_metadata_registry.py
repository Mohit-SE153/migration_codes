"""
Stage 1 tests for lakebridge_discovery.catalog_metadata: the registry/
allowlist parsing, run-mode gating, stats recomputation, and connection-/
probe-level failure isolation -- all exercised without a real SQL Server
(the registry is empty at this stage; later stages add tests specific to
each probe module alongside that probe).
"""
from __future__ import annotations

import pytest

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import _count_all_lists, _select_active_probes
from lakebridge_discovery.catalog_metadata.connection import _build_connection_string
from lakebridge_discovery.config import LakebridgeConfig, SqlServerConfig
from lakebridge_discovery.schema import LakebridgeDependencyRef, LakebridgeDiscoveryResult, LakebridgeObjectRef


def _config(**overrides) -> LakebridgeConfig:
    base = dict(
        enabled=True,
        run_mode="live",
        source=SqlServerConfig(host="db.example", database="AdventureWorks2022", username="u", password="p", use_integrated_auth=False),
        dtsx_fallback_dir=None,
        cli_path="databricks",
        source_tech_sql="MS SQL Server",
        source_tech_etl="SSIS",
        generate_json=True,
        analyze_timeout_seconds=1800,
        output_dir="./output_lakebridge",
        source_export_dir="./output_lakebridge/_source_export",
        catalog_metadata_sources="*",
    )
    base.update(overrides)
    return LakebridgeConfig(**base)


# ---------------------------------------------------------------------------
# _select_active_probes -- pure parsing logic, no DB/registry needed
# ---------------------------------------------------------------------------

def test_select_active_probes_star_selects_everything(monkeypatch):
    fake_registry = [("foo", lambda *a: None), ("bar", lambda *a: None)]
    monkeypatch.setattr(catalog_metadata, "_REGISTRY", fake_registry)
    assert _select_active_probes("*") == fake_registry


@pytest.mark.parametrize("value", ["", "none", "None", "  ", None])
def test_select_active_probes_empty_or_none_selects_nothing(monkeypatch, value):
    monkeypatch.setattr(catalog_metadata, "_REGISTRY", [("foo", lambda *a: None)])
    assert _select_active_probes(value) == []


def test_select_active_probes_honors_explicit_allowlist(monkeypatch):
    foo, bar = ("foo", lambda *a: None), ("bar", lambda *a: None)
    monkeypatch.setattr(catalog_metadata, "_REGISTRY", [foo, bar])
    assert _select_active_probes("bar") == [bar]
    assert _select_active_probes("foo, bar") == [foo, bar]
    assert _select_active_probes("unknown_probe") == []


# ---------------------------------------------------------------------------
# connection string builder -- pure, no pyodbc/network needed
# ---------------------------------------------------------------------------

def test_build_connection_string_uses_sql_auth():
    config = _config()
    conn_str = _build_connection_string(config)
    assert "SERVER=db.example" in conn_str
    assert "DATABASE=AdventureWorks2022" in conn_str
    assert "UID=u" in conn_str
    assert "PWD=p" in conn_str
    assert "Trusted_Connection" not in conn_str


def test_build_connection_string_uses_integrated_auth():
    config = _config(source=SqlServerConfig(
        host="db.example", database="AdventureWorks2022", username=None, password=None, use_integrated_auth=True,
    ))
    conn_str = _build_connection_string(config)
    assert "Trusted_Connection=yes" in conn_str
    assert "UID=" not in conn_str


# ---------------------------------------------------------------------------
# run() orchestration -- registry/connection are monkeypatched, no real DB
# ---------------------------------------------------------------------------

def test_run_is_a_noop_in_fixture_mode_even_with_registered_probes(monkeypatch):
    calls = []
    monkeypatch.setattr(catalog_metadata, "_REGISTRY", [("foo", lambda *a: calls.append(a))])
    monkeypatch.setattr(catalog_metadata, "connect", lambda config: pytest.fail("must not connect in fixture mode"))

    result = LakebridgeDiscoveryResult()
    catalog_metadata.run(_config(run_mode="fixture"), result)

    assert calls == []
    assert result.dependencies == []
    assert result.dependency_stats["total_dependencies"] == 0


def test_run_is_a_noop_when_sources_disabled(monkeypatch):
    connected = []
    monkeypatch.setattr(catalog_metadata, "_REGISTRY", [("foo", lambda *a: pytest.fail("must not run"))])
    monkeypatch.setattr(catalog_metadata, "connect", lambda config: connected.append(1))

    result = LakebridgeDiscoveryResult()
    catalog_metadata.run(_config(catalog_metadata_sources="none"), result)

    assert connected == []


def test_run_recomputes_stats_from_existing_dependencies_when_registry_empty():
    result = LakebridgeDiscoveryResult()
    result.dependencies = [
        LakebridgeDependencyRef(
            source_object="Sales.vStoreWithDemographics", target_object="sales.store", relationship_type="reads",
            source_type="view", target_type="table", discovery_method="lakebridge", resolved=True,
        ),
    ]
    catalog_metadata.run(_config(), result)

    assert result.dependency_stats["total_dependencies"] == 1
    assert result.dependency_stats["by_discovery_method"] == {"lakebridge": 1}


def test_run_records_warning_and_recomputes_stats_when_connection_fails(monkeypatch):
    monkeypatch.setattr(catalog_metadata, "_REGISTRY", [("foo", lambda *a: pytest.fail("must not run"))])

    def _boom(config):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(catalog_metadata, "connect", _boom)

    result = LakebridgeDiscoveryResult()
    catalog_metadata.run(_config(), result)

    assert any("could not establish SQL Server connection" in w for w in result.warnings)
    assert result.dependency_stats["total_dependencies"] == 0


def test_run_isolates_a_failing_probe_and_still_runs_the_next_one(monkeypatch):
    ran = []

    def _failing_probe(connection, result, seen_edges):
        raise ValueError("boom")

    def _working_probe(connection, result, seen_edges):
        ran.append("working")
        result.dependencies.append(LakebridgeDependencyRef(
            source_object="Sales.SalesOrderHeader", target_object="Sales.SalesTerritory",
            relationship_type="foreign_key", source_type="table", target_type="table",
            discovery_method="catalog_metadata", resolved=True,
        ))

    class _FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(catalog_metadata, "_REGISTRY", [("failing", _failing_probe), ("working", _working_probe)])
    monkeypatch.setattr(catalog_metadata, "connect", lambda config: _FakeConnection())

    result = LakebridgeDiscoveryResult()
    catalog_metadata.run(_config(), result)

    assert ran == ["working"]
    assert any("failing" in w and "boom" in w for w in result.warnings)
    assert result.dependency_stats["total_dependencies"] == 1
    assert result.dependency_stats["by_discovery_method"] == {"catalog_metadata": 1}


def test_run_closes_connection_even_if_a_probe_raises(monkeypatch):
    closed = []

    class _FakeConnection:
        def close(self):
            closed.append(True)

    def _failing_probe(connection, result, seen_edges):
        raise ValueError("boom")

    monkeypatch.setattr(catalog_metadata, "_REGISTRY", [("failing", _failing_probe)])
    monkeypatch.setattr(catalog_metadata, "connect", lambda config: _FakeConnection())

    result = LakebridgeDiscoveryResult()
    catalog_metadata.run(_config(), result)

    assert closed == [True]


# ---------------------------------------------------------------------------
# _count_all_lists -- generic per-probe growth accounting, needed because
# indexes.py/constraints.py/sequences.py are inventory-only probes that
# never touch result.dependencies (see catalog_metadata/__init__.py's
# per-probe log line).
# ---------------------------------------------------------------------------

def test_count_all_lists_sums_dependencies_and_object_inventory_together():
    result = LakebridgeDiscoveryResult()
    result.tables = [LakebridgeObjectRef(object_type="table", name="Sales.Store", source_tech="MS SQL Server")]
    result.indexes = [
        LakebridgeObjectRef(object_type="index", name="Sales.Store.PK_Store", source_tech="MS SQL Server"),
        LakebridgeObjectRef(object_type="index", name="Sales.Store.IX_Store_Name", source_tech="MS SQL Server"),
    ]
    result.dependencies = [LakebridgeDependencyRef(
        source_object="Sales.Store", target_object="sales.salesterritory", relationship_type="foreign_key",
        source_type="table", target_type="table", discovery_method="catalog_metadata", resolved=True,
    )]

    assert _count_all_lists(result) == 1 + 2 + 1  # tables + indexes + dependencies


def test_count_all_lists_reflects_inventory_only_probe_growth(monkeypatch):
    """A probe that only appends to result.indexes (never result.dependencies)
    must still be visible in the before/after delta run() computes -- this is
    exactly the gap _count_all_lists was introduced to close."""
    def _inventory_only_probe(connection, result, seen_edges):
        result.indexes.append(LakebridgeObjectRef(object_type="index", name="dbo.T.IX_1", source_tech="MS SQL Server"))
        result.indexes.append(LakebridgeObjectRef(object_type="index", name="dbo.T.IX_2", source_tech="MS SQL Server"))

    class _FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(catalog_metadata, "_REGISTRY", [("inventory_only", _inventory_only_probe)])
    monkeypatch.setattr(catalog_metadata, "connect", lambda config: _FakeConnection())

    result = LakebridgeDiscoveryResult()
    before = _count_all_lists(result)
    catalog_metadata.run(_config(), result)

    assert len(result.indexes) == 2
    assert len(result.dependencies) == 0  # unaffected, as required
    assert _count_all_lists(result) - before == 2
