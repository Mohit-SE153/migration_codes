"""
Fixture-mode catalog data for the pilot sample environment ("SalesDW").

IMPORTANT: this module exists ONLY to make the pipeline runnable and
testable without a live SQL Server connection (no such environment is
reachable from this build). It is not part of the production extraction
path -- in `live` run mode, sql_metadata_extractor and ssis_catalog_extractor
query the real system catalog views instead of anything in this file.

Table/view/procedure structure and stored-proc source text are parsed out
of fixtures/sql/ddl_sample.sql (single source of truth, so fixture data
can't drift from the DDL). Row counts and storage sizes CANNOT be derived
from DDL in a real system (they require sys.dm_db_partition_stats /
sys.database_files) -- here they are synthesized deterministically so
repeated runs are stable, clearly marked as synthetic.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

DDL_PATH = Path(__file__).parent / "sql" / "ddl_sample.sql"


@dataclass
class MockColumn:
    name: str
    data_type: str
    nullable: bool
    ordinal_position: int


@dataclass
class MockTable:
    schema: str
    name: str
    columns: list[MockColumn] = field(default_factory=list)


@dataclass
class MockProcedure:
    schema: str
    name: str
    definition: str


@dataclass
class MockView:
    schema: str
    name: str
    definition: str


@dataclass
class MockTrigger:
    schema: str
    name: str
    table: str
    event: str


def _synthetic_metric(seed: str, low: int, high: int) -> int:
    """Deterministic pseudo-random int in [low, high] derived from a name hash."""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return low + (int(digest[:8], 16) % (high - low + 1))


def _split_statements(raw_sql: str) -> list[str]:
    # ddl_sample.sql uses `GO` batch separators (T-SQL convention, not
    # valid ANSI SQL, so we split on it before handing statements to
    # sqlglot rather than parsing the whole file in one call).
    batches = re.split(r"^\s*GO\s*$", raw_sql, flags=re.MULTILINE)
    return [b.strip() for b in batches if b.strip()]


class MockCatalog:
    def __init__(self, ddl_path: Path = DDL_PATH):
        self.database_name = "SalesDW"
        self.tables: dict[str, MockTable] = {}
        self.procedures: dict[str, MockProcedure] = {}
        self.views: dict[str, MockView] = {}
        self.triggers: list[MockTrigger] = []
        self.foreign_keys: list[tuple[str, str]] = []  # (from "schema.table", to "schema.table")
        self._parse(ddl_path.read_text(encoding="utf-8"))

    def _parse(self, raw_sql: str) -> None:
        for batch in _split_statements(raw_sql):
            try:
                statements = sqlglot.parse(batch, read="tsql")
            except Exception:
                statements = [None]
            for stmt in statements:
                if stmt is None:
                    continue
                if isinstance(stmt, exp.Create):
                    self._handle_create(stmt, batch)
            # CREATE TRIGGER T-SQL syntax isn't part of sqlglot's tsql
            # grammar (falls back to an opaque Command node), so triggers
            # are extracted from the raw batch text directly rather than
            # via the AST -- a real-world case of the "unparseable
            # construct" pattern the hybrid hands to XML/LLM fallback
            # for SSIS, applied here to a SQL-side edge case.
            if re.match(r"\s*CREATE TRIGGER", batch, re.IGNORECASE):
                self._handle_trigger(batch)

    def _handle_create(self, stmt: exp.Create, raw_batch: str) -> None:
        kind = stmt.args.get("kind", "").upper()
        this = stmt.this

        if kind == "TABLE" and isinstance(this, exp.Schema):
            table_exp = this.this
            schema_name = table_exp.db or "dbo"
            table_name = table_exp.name
            columns = []
            for idx, col_def in enumerate((c for c in this.expressions if isinstance(c, exp.ColumnDef)), start=1):
                dtype = col_def.args.get("kind")
                nullable = not any(
                    isinstance(c, exp.ColumnConstraint) and isinstance(c.kind, exp.NotNullColumnConstraint)
                    for c in col_def.args.get("constraints", [])
                )
                columns.append(
                    MockColumn(
                        name=col_def.this.name,
                        data_type=dtype.sql(dialect="tsql") if dtype else "UNKNOWN",
                        nullable=nullable,
                        ordinal_position=idx,
                    )
                )
            key = f"{schema_name}.{table_name}"
            self.tables[key] = MockTable(schema=schema_name, name=table_name, columns=columns)

            for col_def in (c for c in this.expressions if isinstance(c, exp.ColumnDef)):
                for constraint in col_def.args.get("constraints", []):
                    if isinstance(constraint, exp.ColumnConstraint) and isinstance(constraint.kind, exp.Reference):
                        ref_table = constraint.kind.this.this if isinstance(constraint.kind.this, exp.Schema) else constraint.kind.this
                        ref_schema = ref_table.db or "dbo"
                        self.foreign_keys.append((key, f"{ref_schema}.{ref_table.name}"))

        elif kind == "VIEW":
            schema_name = this.db or "dbo"
            view_name = this.name
            key = f"{schema_name}.{view_name}"
            self.views[key] = MockView(schema=schema_name, name=view_name, definition=raw_batch)

        elif kind == "PROCEDURE":
            # CREATE PROCEDURE wraps its target in a StoredProcedure node
            # (`this.this` is the actual Table), unlike CREATE VIEW/TABLE
            # where `this` is the Table/Schema directly.
            proc_table = this.this if isinstance(this, exp.StoredProcedure) else this
            schema_name = proc_table.db or "dbo"
            proc_name = proc_table.name
            key = f"{schema_name}.{proc_name}"
            self.procedures[key] = MockProcedure(schema=schema_name, name=proc_name, definition=raw_batch)


    def _handle_trigger(self, raw_batch: str) -> None:
        m = re.search(
            r"CREATE TRIGGER\s+(?:\[?(\w+)\]?\.)?\[?(\w+)\]?\s+ON\s+(?:\[?(\w+)\]?\.)?\[?(\w+)\]?\s+(AFTER|INSTEAD OF)\s+(\w+)",
            raw_batch, re.IGNORECASE,
        )
        if m:
            trig_schema, trig_name, tbl_schema, tbl_name, _, event = m.groups()
            self.triggers.append(
                MockTrigger(
                    schema=trig_schema or "dbo",
                    name=trig_name,
                    table=f"{tbl_schema or 'dbo'}.{tbl_name}",
                    event=event.upper(),
                )
            )

    def row_count(self, schema: str, table: str) -> int:
        return _synthetic_metric(f"{schema}.{table}:rows", 500, 480_000)

    def size_mb(self, schema: str, table: str) -> float:
        return round(_synthetic_metric(f"{schema}.{table}:size", 4, 2048) / 1.7, 2)

    def database_size_mb(self) -> float:
        return round(sum(self.size_mb(t.schema, t.name) for t in self.tables.values()), 2)

    def data_file_size_mb(self) -> float:
        return round(self.database_size_mb() * 0.8, 2)

    def log_file_size_mb(self) -> float:
        return round(self.database_size_mb() * 0.2, 2)

    def data_occupied_pct(self) -> float:
        return round(self.data_file_size_mb() / max(self.database_size_mb(), 1) * 100.0, 2)

    def log_occupied_pct(self) -> float:
        return round(self.log_file_size_mb() / max(self.database_size_mb(), 1) * 100.0, 2)

    def agent_jobs(self) -> list[dict]:
        # Representative SQL Agent job wired to run the master SSIS
        # package nightly, plus a standalone maintenance job.
        return [
            {
                "name": "Nightly_ETL_Load",
                "enabled": True,
                "steps": [
                    "Execute SSIS Package: Pkg_Master.dtsx",
                    "EXEC dbo.usp_ValidateOrderTotals",
                ],
            },
            {
                "name": "Weekly_Index_Maintenance",
                "enabled": True,
                "steps": ["EXEC dbo.usp_RebuildIndexes"],
            },
        ]
