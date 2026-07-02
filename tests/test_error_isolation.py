"""
Verifies the core non-functional requirement: one broken object must not
take down the whole discovery run. These tests inject deliberately broken
inputs (malformed XML, a query method that raises) and assert the run
still completes, with the failure surfaced as an individual log entry.
"""
import os
import shutil

import pytest

from autovista.logging_setup import configure_logging
from autovista.ssis_catalog_extractor import FileSystemDtsxSource, extract_ssis_packages
from autovista.sql_metadata_extractor import FixtureMetadataSource, extract_database_metadata
from fixtures.mock_catalog import MockCatalog


@pytest.fixture(autouse=True)
def _logging(tmp_path):
    configure_logging(str(tmp_path))


def test_one_broken_dtsx_file_does_not_fail_the_run(tmp_path):
    for name in ("Pkg_LoadCustomers.dtsx", "Pkg_Master.dtsx"):
        shutil.copy(f"fixtures/dtsx/{name}", tmp_path / name)
    (tmp_path / "Pkg_Broken.dtsx").write_text("<DTS:Executable this is not valid xml <<<", encoding="utf-8")

    source = FileSystemDtsxSource(directory=str(tmp_path))
    packages, log_entries = extract_ssis_packages(source, deployment_model="file_system")

    parsed_names = {p.name for p in packages}
    assert "Pkg_LoadCustomers" in parsed_names
    assert "Pkg_Master" in parsed_names
    assert "Pkg_Broken" not in parsed_names

    failed = [e for e in log_entries if e.status == "failed"]
    assert len(failed) == 1
    assert failed[0].object_name == "Pkg_Broken"
    assert failed[0].error  # error message captured, not swallowed


class _ExplodingTriggerSource(FixtureMetadataSource):
    def list_triggers(self, database: str):
        raise RuntimeError("simulated DMV timeout")


def test_broken_metadata_query_does_not_fail_other_extractions():
    source = _ExplodingTriggerSource(catalog=MockCatalog())
    result, log_entries = extract_database_metadata(source, database="SalesDW")

    # trigger extraction failed...
    assert result["triggers"] == []
    trigger_entry = next(e for e in log_entries if e.object_type == "trigger")
    assert trigger_entry.status == "failed"

    # ...but everything else still succeeded.
    assert len(result["tables"]) == 21
    assert len(result["stored_procedures"]) == 11
    table_entry = next(e for e in log_entries if e.object_type == "table")
    assert table_entry.status == "success"
