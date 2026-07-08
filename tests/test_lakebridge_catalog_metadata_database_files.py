"""
Tests for lakebridge_discovery.catalog_metadata.database_files -- pure
object-inventory discovery from sys.database_files only. Exercised against
a stub connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import database_files
from lakebridge_discovery.schema import LakebridgeDiscoveryResult


class _FakeCursor:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def execute(self, sql: str):
        return self

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


def test_discover_emits_data_file_with_mb_growth():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([
        ("AdventureWorks2022", "D:\\data\\AdventureWorks2022.mdf", "ROWS", 102400.0, None, False, 8192, "PRIMARY"),
    ])

    database_files.discover(connection, result, seen_edges=set())

    assert len(result.database_files) == 1
    f = result.database_files[0]
    assert f.logical_name == "AdventureWorks2022"
    assert f.physical_name == "D:\\data\\AdventureWorks2022.mdf"
    assert f.file_type == "ROWS"
    assert f.filegroup == "PRIMARY"
    assert f.current_size_mb == 102400.0
    assert f.max_size_mb is None
    assert f.growth_type == "MB"
    assert f.growth_mb == 64.0  # 8192 pages * 8KB / 1024


def test_discover_emits_log_file_with_percent_growth():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([
        ("AdventureWorks2022_log", "D:\\data\\AdventureWorks2022_log.ldf", "LOG", 51200.0, 2097152.0, True, 10, ""),
    ])

    database_files.discover(connection, result, seen_edges=set())

    f = result.database_files[0]
    assert f.file_type == "LOG"
    assert f.filegroup is None  # log files have no filegroup
    assert f.max_size_mb == 2097152.0
    assert f.growth_type == "PERCENT"
    assert f.growth_mb == 10.0


def test_discover_deduplicates_identical_logical_names():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([
        ("AdventureWorks2022", "D:\\data\\AdventureWorks2022.mdf", "ROWS", 100.0, None, False, 128, "PRIMARY"),
        ("AdventureWorks2022", "D:\\data\\AdventureWorks2022.mdf", "ROWS", 100.0, None, False, 128, "PRIMARY"),
    ])

    database_files.discover(connection, result, seen_edges=set())

    assert len(result.database_files) == 1


def test_discover_does_not_touch_dependencies_or_other_categories():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection([("A", "B", "ROWS", 1.0, None, False, 128, "PRIMARY")])

    database_files.discover(connection, result, seen_edges=set())

    assert result.dependencies == []
    assert result.tables == []


def test_database_files_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "database_files" in names
