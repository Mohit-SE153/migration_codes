"""Tests for assessment.data_readiness."""
from __future__ import annotations

from assessment.data_readiness import build_data_readiness


def test_no_data_quality_summary_produces_no_findings():
    assert build_data_readiness({}) == []
    assert build_data_readiness({"data_quality_summary": []}) == []


def test_zero_counts_are_not_reported():
    manifest = {"data_quality_summary": [{"database": "db", "tables_without_primary_key": 0, "heap_tables": 0}]}
    findings = build_data_readiness(manifest)
    assert findings == []


def test_cdc_enabled_tables_reported_as_high_severity():
    manifest = {"data_quality_summary": [{"database": "db", "tables_with_cdc_enabled": 3}]}
    findings = build_data_readiness(manifest)
    assert len(findings) == 1
    assert findings[0].category == "tables_with_cdc_enabled"
    assert findings[0].count == 3
    assert findings[0].severity == "High"


def test_counts_summed_across_multiple_database_summaries():
    manifest = {"data_quality_summary": [
        {"database": "db1", "heap_tables": 2},
        {"database": "db2", "heap_tables": 5},
    ]}
    findings = build_data_readiness(manifest)
    heap_finding = next(f for f in findings if f.category == "heap_tables")
    assert heap_finding.count == 7


def test_sample_list_fields_populate_sample_objects():
    manifest = {"data_quality_summary": [{"database": "db", "wide_schema_tables": ["dbo.Wide1", "dbo.Wide2"]}]}
    findings = build_data_readiness(manifest)
    assert len(findings) == 1
    assert findings[0].category == "wide_schema_tables"
    assert findings[0].count == 2
    assert findings[0].sample_objects == ["dbo.Wide1", "dbo.Wide2"]


def test_filestream_tables_are_critical_severity():
    manifest = {"data_quality_summary": [{"database": "db", "tables_with_filestream": 1}]}
    findings = build_data_readiness(manifest)
    assert findings[0].severity == "Critical"
