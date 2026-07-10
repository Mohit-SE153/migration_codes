"""Tests for lakebridge_assessment.risk_register."""
from __future__ import annotations

from lakebridge_assessment.risk_register import _parse_notes_kv, build_risk_register


def test_unsupported_object_becomes_high_severity_finding():
    manifest = {"unsupported_objects": [{"object_type": "view", "name": "dbo.Weird", "notes": "some note"}]}
    findings = build_risk_register(manifest)
    assert len(findings) == 1
    assert findings[0].category == "LAKEBRIDGE_UNSUPPORTED"
    assert findings[0].severity == "High"
    assert findings[0].description == "some note"


def test_unsupported_object_with_no_notes_gets_generic_description():
    manifest = {"unsupported_objects": [{"object_type": "view", "name": "dbo.Weird"}]}
    findings = build_risk_register(manifest)
    assert "no further detail" in findings[0].description


def test_linked_server_flag_is_critical_severity():
    manifest = {"unsupported_objects": [], "tables": [{"name": "dbo.Remote", "compatibility_flags": ["LINKED_SERVER"]}],
                "views": [], "stored_procedures": [], "functions": [], "triggers": []}
    findings = build_risk_register(manifest)
    assert findings[0].severity == "Critical"


def test_parse_notes_kv_extracts_permission_set():
    assert _parse_notes_kv("permission_set=UNSAFE_ACCESS;is_visible=True") == {
        "permission_set": "UNSAFE_ACCESS", "is_visible": "True",
    }


def test_parse_notes_kv_handles_none():
    assert _parse_notes_kv(None) == {}


def test_unsafe_clr_assembly_is_critical():
    manifest = {"unsupported_objects": [], "tables": [], "views": [], "stored_procedures": [], "functions": [],
                "triggers": [], "assemblies": [{"name": "X", "notes": "permission_set=UNSAFE_ACCESS;is_visible=True"}]}
    findings = build_risk_register(manifest)
    assert len(findings) == 1
    assert findings[0].category == "CLR_ASSEMBLY"
    assert findings[0].severity == "Critical"


def test_linked_server_entity_is_critical():
    manifest = {"unsupported_objects": [], "tables": [], "views": [], "stored_procedures": [], "functions": [],
                "triggers": [], "assemblies": [], "linked_servers": [{"name": "REMOTE1", "product": "Oracle"}]}
    findings = build_risk_register(manifest)
    assert findings[0].category == "LINKED_SERVER"
    assert findings[0].severity == "Critical"


def test_empty_manifest_produces_no_findings():
    assert build_risk_register({}) == []
