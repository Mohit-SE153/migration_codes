"""Tests for lakebridge_assessment.data_readiness."""
from __future__ import annotations

from lakebridge_assessment.data_readiness import build_data_readiness


def test_no_data_quality_summary_produces_no_count_findings():
    assert build_data_readiness({}) == []


def test_cdc_enabled_tables_reported_as_high_severity():
    manifest = {"data_quality_summary": [{"database": "db", "tables_with_cdc_enabled": 3}]}
    findings = build_data_readiness(manifest)
    assert len(findings) == 1
    assert findings[0].severity == "High"


def test_memory_optimized_tables_come_from_table_features_not_data_quality_summary():
    manifest = {"data_quality_summary": [], "table_features": [
        {"schema": "dbo", "name": "FastTable", "is_memory_optimized": True},
        {"schema": "dbo", "name": "NormalTable", "is_memory_optimized": False},
    ]}
    findings = build_data_readiness(manifest)
    assert len(findings) == 1
    assert findings[0].category == "tables_with_memory_optimized"
    assert findings[0].severity == "Critical"
    assert findings[0].sample_objects == ["dbo.FastTable"]


def test_no_filestream_or_spatial_fields_exist_in_lakebridge_schema():
    # Lakebridge's DataQualitySummaryEntity has no filestream/spatial/xml
    # fields at all -- passing them should simply be ignored, not error.
    manifest = {"data_quality_summary": [{"database": "db", "tables_with_filestream": 5}]}
    findings = build_data_readiness(manifest)
    assert findings == []
