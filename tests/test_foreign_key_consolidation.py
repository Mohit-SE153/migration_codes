"""
Tests for Discovery Phase 2.7: consolidating the two independent
foreign-key queries (QUERY_FOREIGN_KEYS vs. QUERY_FOREIGN_KEY_CONSTRAINTS)
onto a single source of truth. list_foreign_keys() must now be a thin
derivation over the same FK rows list_constraints() produces, for both
LiveSqlServerSource and FixtureMetadataSource -- this is the regression
test that would have caught future drift between the two, per the task
brief.
"""
from __future__ import annotations

from autovista.sql_metadata_extractor import FixtureMetadataSource, LiveSqlServerSource
from fixtures.mock_catalog import MockCatalog


class FakeCursor:
    def __init__(self, responses: list[tuple[str, list[tuple]]]):
        self._responses = responses
        self._rows: list[tuple] = []

    def execute(self, sql, *params):
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
    def __init__(self, responses: list[tuple[str, list[tuple]]]):
        self.responses = responses

    def cursor(self):
        return FakeCursor(self.responses)


def _source(responses: list[tuple[str, list[tuple]]]) -> LiveSqlServerSource:
    return LiveSqlServerSource(connection=FakeConnection(responses))


def test_live_list_foreign_keys_agrees_with_fk_rows_in_list_constraints():
    source = _source([
        ("FROM sys.foreign_keys fk", [
            ("dbo", "Orders", "FK_Orders_Customers", "dbo", "Customers", "CustomerId", "CustomerId", 1,
             "CASCADE", "NO_ACTION", False, False, False),
            ("dbo", "OrderDetails", "FK_OrderDetails_Orders", "dbo", "Orders", "OrderId", "OrderId", 1,
             "NO_ACTION", "NO_ACTION", False, False, False),
        ]),
    ])
    tuples = source.list_foreign_keys("SalesDW")
    constraints = source.list_constraints("SalesDW")
    fk_constraints = [c for c in constraints if c.constraint_type == "FOREIGN_KEY"]

    assert set(tuples) == {(f"{c.schema}.{c.table}", c.referenced_table) for c in fk_constraints}
    assert len(tuples) == len(fk_constraints) == 2


def test_live_list_foreign_keys_collapses_composite_key_fk_to_one_tuple():
    """A composite-key FK spans multiple sys.foreign_key_columns rows but
    must still collapse to exactly one (from, to) tuple -- same grouping
    logic list_constraints() already applies via _fetch_foreign_key_constraints."""
    source = _source([
        ("FROM sys.foreign_keys fk", [
            ("dbo", "OrderDetails", "FK_OrderDetails_Composite", "dbo", "Products",
             "OrderId", "OrderId", 1, "NO_ACTION", "NO_ACTION", False, False, False),
            ("dbo", "OrderDetails", "FK_OrderDetails_Composite", "dbo", "Products",
             "ProductId", "ProductId", 2, "NO_ACTION", "NO_ACTION", False, False, False),
        ]),
    ])
    tuples = source.list_foreign_keys("SalesDW")
    assert tuples == [("dbo.OrderDetails", "dbo.Products")]


def test_live_list_foreign_keys_and_list_constraints_issue_the_same_underlying_query():
    """Regression guard for the specific bug class this consolidation
    fixes: list_foreign_keys() must read from QUERY_FOREIGN_KEY_CONSTRAINTS
    (the same query list_constraints() uses), not a second independent
    QUERY_FOREIGN_KEYS -- proven by only ever registering a response for
    the "sys.foreign_keys fk" / "sys.foreign_key_columns" shaped
    constraint query and confirming list_foreign_keys() still resolves
    correctly (a second, differently-shaped query would return no rows
    against this fixture and the test would fail)."""
    source = _source([
        ("JOIN sys.foreign_key_columns fkc", [
            ("dbo", "Orders", "FK_Orders_Customers", "dbo", "Customers", "CustomerId", "CustomerId", 1,
             "CASCADE", "NO_ACTION", False, False, False),
        ]),
    ])
    tuples = source.list_foreign_keys("SalesDW")
    assert tuples == [("dbo.Orders", "dbo.Customers")]


def test_fixture_list_foreign_keys_agrees_with_fk_rows_in_list_constraints():
    source = FixtureMetadataSource(catalog=MockCatalog())
    tuples = source.list_foreign_keys("SalesDW")
    constraints = source.list_constraints("SalesDW")
    fk_constraints = [c for c in constraints if c.constraint_type == "FOREIGN_KEY"]

    assert set(tuples) == {(f"{c.schema}.{c.table}", c.referenced_table) for c in fk_constraints}
    assert len(tuples) == len(fk_constraints)
    assert len(tuples) > 0
