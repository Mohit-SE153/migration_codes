"""
Tests for autovista.run_diagnostics.collect_errors/collect_warnings --
derive manifest.errors/manifest.warnings purely from this run's own log
entries and parse_status/unresolved_reason fields, never a new check.
"""
from __future__ import annotations

from autovista.run_diagnostics import collect_errors, collect_warnings
from autovista.schema import DiscoveryLogEntry, DiscoveryManifest, StoredProcedureEntity, ViewEntity


def test_collect_errors_only_includes_failed_entries_with_a_message():
    log_entries = [
        DiscoveryLogEntry(object_type="trigger", object_name="dbo.trgBad", status="failed", error="parse exploded"),
        DiscoveryLogEntry(object_type="table", object_name="dbo.Orders", status="success"),
        DiscoveryLogEntry(object_type="view", object_name="dbo.vX", status="skipped_unchanged"),
    ]
    errors = collect_errors(log_entries)
    assert errors == ["trigger:dbo.trgBad: parse exploded"]


def test_collect_errors_empty_when_no_failures():
    log_entries = [DiscoveryLogEntry(object_type="table", object_name="dbo.Orders", status="success")]
    assert collect_errors(log_entries) == []


def test_collect_warnings_includes_unresolved_and_llm_inferred_procs():
    manifest = DiscoveryManifest()
    manifest.stored_procedures = [
        StoredProcedureEntity(database="SalesDW", schema="dbo", name="usp_Weird", loc=1, parse_status="unresolved", unresolved_reason="dynamic SQL"),
        StoredProcedureEntity(database="SalesDW", schema="dbo", name="usp_Fine", loc=1, parse_status="direct_metadata"),
    ]
    warnings = collect_warnings(manifest)
    assert warnings == ["stored_procedure:dbo.usp_Weird: dynamic SQL"]


def test_collect_warnings_falls_back_to_parse_status_when_no_reason_text():
    manifest = DiscoveryManifest()
    manifest.views = [ViewEntity(database="SalesDW", schema="dbo", name="vBad", parse_status="llm_inferred")]
    warnings = collect_warnings(manifest)
    assert warnings == ["view:dbo.vBad: llm_inferred"]


def test_collect_warnings_empty_when_everything_resolved():
    manifest = DiscoveryManifest()
    manifest.views = [ViewEntity(database="SalesDW", schema="dbo", name="vFine", parse_status="sqlglot")]
    assert collect_warnings(manifest) == []
