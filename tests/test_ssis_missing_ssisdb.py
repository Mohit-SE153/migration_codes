"""
Verifies Task 2's requirement: when SSISDB isn't installed on the target
instance, SSIS discovery must skip gracefully (informational log, empty
package list) rather than raising or being counted as a failed object.
"""
from __future__ import annotations

from autovista.ssis_catalog_extractor import LiveSsisCatalogSource, extract_ssis_packages


class _FakeCursor:
    def __init__(self, ssisdb_installed: bool):
        self._ssisdb_installed = ssisdb_installed

    def execute(self, sql, *params):
        if "SELECT 1 FROM sys.databases WHERE name = 'SSISDB'" in sql:
            self._rows = [(1,)] if self._ssisdb_installed else []
        elif "SSISDB.catalog.projects" in sql:
            if not self._ssisdb_installed:
                raise Exception("Invalid object name 'SSISDB.catalog.projects'.")
            self._rows = [("Pilot", "PilotProject")]
        else:
            self._rows = []

    def fetchall(self):
        return list(getattr(self, "_rows", []))

    def fetchone(self):
        rows = getattr(self, "_rows", [])
        return rows[0] if rows else None


class _FakeConnection:
    def __init__(self, ssisdb_installed: bool):
        self._ssisdb_installed = ssisdb_installed

    def cursor(self):
        return _FakeCursor(self._ssisdb_installed)


def test_list_projects_returns_empty_when_ssisdb_not_installed():
    source = LiveSsisCatalogSource(connection=_FakeConnection(ssisdb_installed=False))
    assert source.list_projects() == []


def test_missing_ssisdb_is_not_treated_as_a_failed_discovery_object():
    source = LiveSsisCatalogSource(connection=_FakeConnection(ssisdb_installed=False))
    packages, log_entries = extract_ssis_packages(source, deployment_model="ssisdb")

    assert packages == []
    assert all(entry.status != "failed" for entry in log_entries)
    project_entry = next(e for e in log_entries if e.object_type == "ssis_project")
    assert project_entry.status == "success"


def test_list_projects_still_queries_catalog_when_ssisdb_installed():
    source = LiveSsisCatalogSource(connection=_FakeConnection(ssisdb_installed=True))
    assert source.list_projects() == [("Pilot", "PilotProject")]
