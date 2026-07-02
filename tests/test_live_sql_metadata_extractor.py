"""
Regression tests for the live-mode system-catalog queries in
sql_metadata_extractor.py. These exercise LiveSqlServerSource against a
fake pyodbc-shaped connection/cursor (no real SQL Server needed) so that
a column-name/column-order bug in a query -- like the ones found during
live validation (is_encrypted, retry_attempts, return_type, min_value/
max_value, schema_collection_id, assemblies.schema_id,
is_snapshot_isolation_on) -- is caught by unit tests instead of only
surfacing against a real instance.
"""
from __future__ import annotations

from autovista.sql_metadata_extractor import LiveSqlServerSource


class FakeCursor:
    def __init__(self, responses: list[tuple[str, list[tuple]]]):
        self._responses = responses
        self._rows: list[tuple] = []
        self.last_sql: str | None = None
        self.last_params: tuple = ()

    def execute(self, sql, *params):
        self.last_sql = sql
        self.last_params = params
        for needle, rows in self._responses:
            if needle in sql:
                self._rows = rows
                return
        self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConnection:
    """Matches each cursor.execute() call against a list of (substring,
    rows) pairs, in order -- first match wins, so register more specific
    substrings before more general ones."""

    def __init__(self, responses: list[tuple[str, list[tuple]]]):
        self.responses = responses

    def cursor(self):
        return FakeCursor(self.responses)


def _source(responses: list[tuple[str, list[tuple]]]) -> LiveSqlServerSource:
    return LiveSqlServerSource(connection=FakeConnection(responses))


def test_list_procedures_reads_is_encrypted_and_execute_as_from_correct_columns():
    source = _source([
        ("FROM sys.procedures p", [
            ("dbo", "usp_GetOrders", "CREATE PROCEDURE dbo.usp_GetOrders AS SELECT 1", "2024-01-01", "2024-06-01", 1, 5, 123),
        ]),
    ])
    procs = source.list_procedures("SalesDW")
    assert len(procs) == 1
    entity, definition = procs[0]
    assert entity.is_encrypted is True
    assert entity.execute_as == "5"
    assert "usp_GetOrders" in definition


def test_list_agent_jobs_reads_retry_settings_from_sysjobsteps_not_sysjobs():
    source = _source([
        ("FROM msdb.dbo.sysjobs j", [
            ("NightlyLoad", 1, "EXEC dbo.usp_Load", b"\x01", "2024-01-01", "2024-06-01", 3, 5, "Nightly ETL job"),
        ]),
    ])
    jobs = source.list_agent_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.retry_attempts == 3
    assert job.retry_interval == 5


def test_list_functions_resolves_return_type_via_parameters_join():
    source = _source([
        ("FROM sys.objects f", [
            ("dbo", "ufn_GetOrderStatus", "SQL_SCALAR_FUNCTION", "nvarchar", 456),
        ]),
    ])
    functions = source.list_functions("SalesDW")
    assert len(functions) == 1
    assert functions[0].return_type == "nvarchar"


def test_list_sequences_maps_minimum_and_maximum_value():
    source = _source([
        ("FROM sys.sequences seq", [
            ("dbo", "Seq_OrderId", 1000, 1, 1, 2147483647, 50),
        ]),
    ])
    sequences = source.list_sequences("SalesDW")
    assert len(sequences) == 1
    seq = sequences[0]
    assert seq.minimum_value == 1
    assert seq.maximum_value == 2147483647


def test_list_xml_schema_collections_does_not_reference_schema_collection_id():
    source = _source([
        ("FROM sys.xml_schema_collections x", [
            ("dbo", "OrderSchema", 7),
        ]),
    ])
    collections = source.list_xml_schema_collections("SalesDW")
    assert len(collections) == 1
    assert collections[0].name == "OrderSchema"


def test_list_assemblies_uses_owner_default_schema_not_schema_id_join():
    source = _source([
        ("FROM sys.assemblies a", [
            ("dbo", "SalesDWCLR", "SAFE", True),
        ]),
    ])
    assemblies = source.list_assemblies("SalesDW")
    assert len(assemblies) == 1
    assembly = assemblies[0]
    assert assembly.schema == "dbo"
    assert assembly.permission_set == "SAFE"


def test_list_indexes_splits_key_and_included_columns():
    source = _source([
        ("FROM sys.indexes i", [
            ("dbo", "Orders", "IX_Orders_CustomerId", "NONCLUSTERED", False, False, False, 90, 1, None, 111, 2),
        ]),
        ("FROM sys.index_columns ic", [
            ("CustomerId", False),
            ("OrderDate", True),
        ]),
    ])
    indexes = source.list_indexes("SalesDW")
    assert len(indexes) == 1
    idx = indexes[0]
    assert idx.key_columns == ["CustomerId"]
    assert idx.included_columns == ["OrderDate"]


def test_list_tables_computes_percent_of_database_and_sorts_largest_first():
    source = _source([
        ("JOIN sys.master_files mf", [("SalesDW", 1000.0)]),
        ("FROM sys.tables t", [
            ("dbo", "SmallTable", "2024-01-01", "2024-06-01", "CLUSTERED", 10, 100.0, 0, 0, 0, 0, 0, 1, 1, 1),
            ("dbo", "BigTable", "2024-01-01", "2024-06-01", "CLUSTERED", 1000, 400.0, 0, 0, 0, 0, 0, 1, 1, 1),
        ]),
        ("FROM sys.columns c", []),
    ])
    tables = source.list_tables("SalesDW")
    assert [t.name for t in tables] == ["BigTable", "SmallTable"]
    assert tables[0].percent_of_database_occupied == 40.0
    assert tables[1].percent_of_database_occupied == 10.0


def test_database_properties_translate_snapshot_isolation_state_desc_to_bool():
    source = _source([
        ("JOIN sys.master_files mf", [("SalesDW", 500.0)]),
        ("FROM sys.databases d", [
            (
                "SalesDW", "FULL", 150, "sa", "SQL_Latin1_General_CP1_CI_AS", "2024-01-01",
                False, False, False, True, "CHECKSUM", "NONE", "ON", True,
            ),
        ]),
        ("FROM msdb.dbo.backupset b", [("2024-06-15", None, "2024-06-16")]),
        ("FROM msdb.dbo.restorehistory rh", [("2024-06-17",)]),
    ])
    databases = source.list_databases()
    assert len(databases) == 1
    db = databases[0]
    assert db.is_snapshot_isolation_on is True
    assert db.is_read_committed_snapshot_on is True
    assert db.last_full_backup == "2024-06-15"
    assert db.last_log_backup == "2024-06-16"
    assert db.last_restore_date == "2024-06-17"


def test_database_properties_backup_query_failure_is_isolated_not_fatal():
    class ExplodingBackupConnection(FakeConnection):
        def cursor(self):
            cursor = super().cursor()
            original_execute = cursor.execute

            def execute(sql, *params):
                if "backupset" in sql:
                    raise RuntimeError("permission denied on msdb")
                return original_execute(sql, *params)

            cursor.execute = execute
            return cursor

    source = LiveSqlServerSource(connection=ExplodingBackupConnection([
        ("JOIN sys.master_files mf", [("SalesDW", 500.0)]),
        ("FROM sys.databases d", [
            (
                "SalesDW", "FULL", 150, "sa", "SQL_Latin1_General_CP1_CI_AS", "2024-01-01",
                False, False, False, True, "CHECKSUM", "NONE", "OFF", True,
            ),
        ]),
    ]))
    databases = source.list_databases()
    assert len(databases) == 1
    assert databases[0].last_full_backup is None
    assert databases[0].is_snapshot_isolation_on is False


def test_list_database_summary_populates_size_and_backup_dates():
    source = _source([
        ("JOIN sys.master_files mf", [("SalesDW", 750.0)]),
        ("FROM sys.databases d", [("SalesDW", "FULL", 150)]),
        ("FROM msdb.dbo.backupset b", [("2024-06-15", None, None)]),
        ("FROM msdb.dbo.restorehistory rh", [(None,)]),
    ])
    summaries = source.list_database_summary("SalesDW")
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.recovery_model == "FULL"
    assert summary.compatibility_level == "150"
    assert summary.last_backup == "2024-06-15"
    assert summary.database_size_mb == 750.0
