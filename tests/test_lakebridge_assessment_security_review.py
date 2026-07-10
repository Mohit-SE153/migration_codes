"""Tests for lakebridge_assessment.security_review."""
from __future__ import annotations

from lakebridge_assessment.security_review import build_security_notes


def test_empty_manifest_produces_no_notes():
    assert build_security_notes({}) == []


def test_sysadmin_server_login_flagged_high_severity():
    manifest = {"server_principals": [{"name": "sa", "member_of_roles": ["sysadmin"]}]}
    notes = build_security_notes(manifest)
    assert len(notes) == 1
    assert notes[0].category == "PRIVILEGED_SERVER_LOGINS"
    assert notes[0].severity == "High"


def test_unsafe_assembly_produces_high_severity_note():
    manifest = {"assemblies": [{"name": "X", "notes": "permission_set=UNSAFE_ACCESS;is_visible=True"}]}
    notes = build_security_notes(manifest)
    assert len(notes) == 1
    assert notes[0].category == "UNSAFE_CLR_ASSEMBLIES"


def test_permissions_combined_from_server_and_database_lists():
    manifest = {
        "server_permissions": [{"grantee": "a", "permission_name": "CONTROL SERVER"}],
        "database_permissions": [{"grantee": "b", "permission_name": "SELECT"}],
    }
    notes = build_security_notes(manifest)
    volume_note = next(n for n in notes if n.category == "PERMISSION_VOLUME")
    assert volume_note.count == 2
    high_priv_note = next(n for n in notes if n.category == "HIGH_PRIVILEGE_GRANTS")
    assert high_priv_note.count == 1
