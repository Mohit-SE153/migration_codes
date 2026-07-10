"""Tests for assessment.security_review."""
from __future__ import annotations

from assessment.security_review import build_security_notes


def test_empty_manifest_produces_no_notes():
    assert build_security_notes({}) == []


def test_sysadmin_server_login_flagged_high_severity():
    manifest = {"security_principals": [
        {"scope": "server", "name": "sa", "member_of_roles": ["sysadmin"]},
        {"scope": "database", "name": "dbo", "member_of_roles": []},
    ]}
    notes = build_security_notes(manifest)
    assert len(notes) == 1
    assert notes[0].category == "PRIVILEGED_SERVER_LOGINS"
    assert notes[0].severity == "High"
    assert "sa" in notes[0].description


def test_non_privileged_server_login_not_flagged():
    manifest = {"security_principals": [{"scope": "server", "name": "svc_account", "member_of_roles": ["public"]}]}
    assert build_security_notes(manifest) == []


def test_linked_servers_produce_medium_severity_note():
    manifest = {"linked_servers": [{"name": "REMOTE1"}, {"name": "REMOTE2"}]}
    notes = build_security_notes(manifest)
    assert len(notes) == 1
    assert notes[0].category == "LINKED_SERVER_CREDENTIALS"
    assert notes[0].count == 2
    assert notes[0].severity == "Medium"


def test_unsafe_assembly_produces_high_severity_note():
    manifest = {"assemblies": [{"name": "X", "permission_set": "UNSAFE_ACCESS"}]}
    notes = build_security_notes(manifest)
    assert notes[0].category == "UNSAFE_CLR_ASSEMBLIES"
    assert notes[0].severity == "High"


def test_safe_assembly_produces_no_note():
    manifest = {"assemblies": [{"name": "X", "permission_set": "SAFE"}]}
    assert build_security_notes(manifest) == []


def test_high_privilege_permission_grants_detected_case_insensitively():
    manifest = {"permissions": [{"grantee": "user1", "permission_name": "Control Server"}]}
    notes = build_security_notes(manifest)
    categories = [n.category for n in notes]
    assert "HIGH_PRIVILEGE_GRANTS" in categories
    assert "PERMISSION_VOLUME" in categories


def test_permission_volume_note_counts_distinct_grantees():
    manifest = {"permissions": [
        {"grantee": "user1", "permission_name": "SELECT"},
        {"grantee": "user1", "permission_name": "SELECT"},
        {"grantee": "user2", "permission_name": "SELECT"},
    ]}
    notes = build_security_notes(manifest)
    volume_note = next(n for n in notes if n.category == "PERMISSION_VOLUME")
    assert volume_note.count == 3
    assert "2 distinct grantee" in volume_note.description
