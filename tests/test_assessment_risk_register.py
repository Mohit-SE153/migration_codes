"""Tests for assessment.risk_register."""
from __future__ import annotations

from assessment.risk_register import build_risk_register


def test_unresolved_object_becomes_high_severity_finding():
    manifest = {"unsupported_objects": [
        {"object_type": "function", "name": "dbo.ufnBroken", "parse_status": "unresolved", "reason": "boom"},
    ]}
    findings = build_risk_register(manifest)
    assert len(findings) == 1
    assert findings[0].category == "PARSE_UNRESOLVED"
    assert findings[0].severity == "High"


def test_partial_parse_becomes_medium_severity_finding():
    manifest = {"unsupported_objects": [
        {"object_type": "trigger", "name": "dbo.Trg", "parse_status": "sqlglot", "reason": "fell back to Command node"},
    ]}
    findings = build_risk_register(manifest)
    assert findings[0].category == "PARSE_PARTIAL"
    assert findings[0].severity == "Medium"


def test_duplicate_unsupported_object_rows_collapse_into_one_finding_with_note():
    manifest = {"unsupported_objects": [
        {"object_type": "trigger", "name": "Sales.iduSalesOrderDetail", "parse_status": "unresolved", "reason": "same error"},
        {"object_type": "trigger", "name": "Sales.iduSalesOrderDetail", "parse_status": "unresolved", "reason": "same error"},
        {"object_type": "trigger", "name": "Sales.iduSalesOrderDetail", "parse_status": "unresolved", "reason": "same error"},
    ]}
    findings = build_risk_register(manifest)
    assert len(findings) == 1
    assert "3 times" in findings[0].remediation


def test_linked_server_construct_flag_is_critical_severity():
    manifest = {"unsupported_objects": [], "stored_procedures": [
        {"schema": "dbo", "name": "usp_Remote", "compatibility_flags": ["LINKED_SERVER"], "compatibility_notes": None},
    ], "views": [], "functions": [], "triggers": [], "packages": []}
    findings = build_risk_register(manifest)
    assert len(findings) == 1
    assert findings[0].severity == "Critical"
    assert findings[0].category == "COMPATIBILITY_FLAGS"


def test_pivot_only_flag_is_low_severity_not_critical():
    manifest = {"unsupported_objects": [], "stored_procedures": [], "views": [
        {"schema": "dbo", "name": "vPivoted", "compatibility_flags": ["PIVOT"], "compatibility_notes": None},
    ], "functions": [], "triggers": [], "packages": []}
    findings = build_risk_register(manifest)
    assert findings[0].severity == "Low"


def test_mixed_flags_take_the_worst_severity():
    manifest = {"unsupported_objects": [], "stored_procedures": [], "views": [], "functions": [], "triggers": [
        {"schema": "dbo", "name": "Trg", "compatibility_flags": ["PIVOT", "XP_CMDSHELL"], "compatibility_notes": None},
    ], "packages": []}
    findings = build_risk_register(manifest)
    assert findings[0].severity == "Critical"


def test_unsafe_clr_assembly_flagged_critical():
    manifest = {"unsupported_objects": [], "stored_procedures": [], "views": [], "functions": [], "triggers": [],
                "packages": [], "assemblies": [{"schema": None, "name": "My.Assembly", "permission_set": "UNSAFE_ACCESS"}]}
    findings = build_risk_register(manifest)
    assert len(findings) == 1
    assert findings[0].category == "CLR_ASSEMBLY"
    assert findings[0].severity == "Critical"


def test_safe_clr_assembly_flagged_high_not_critical():
    manifest = {"unsupported_objects": [], "stored_procedures": [], "views": [], "functions": [], "triggers": [],
                "packages": [], "assemblies": [{"schema": None, "name": "My.Assembly", "permission_set": "SAFE"}]}
    findings = build_risk_register(manifest)
    assert findings[0].severity == "High"


def test_linked_server_entity_produces_critical_finding():
    manifest = {"unsupported_objects": [], "stored_procedures": [], "views": [], "functions": [], "triggers": [],
                "packages": [], "assemblies": [], "linked_servers": [{"name": "REMOTE1", "product": "Oracle"}]}
    findings = build_risk_register(manifest)
    assert len(findings) == 1
    assert findings[0].category == "LINKED_SERVER"
    assert findings[0].severity == "Critical"


def test_empty_manifest_produces_no_findings():
    assert build_risk_register({}) == []
