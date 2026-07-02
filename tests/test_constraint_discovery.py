"""
Tests for Discovery Enhancement 2: constraint discovery (primary key,
unique, foreign key, check, default) via LiveSqlServerSource.list_constraints,
plus fixture-mode parity.
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


def test_composite_primary_key_columns_are_grouped_under_one_constraint():
    source = _source([
        ("FROM sys.key_constraints kc", [
            ("dbo", "OrderDetails", "PK_OrderDetails", "PK", True, "OrderId", 1),
            ("dbo", "OrderDetails", "PK_OrderDetails", "PK", True, "ProductId", 2),
        ]),
    ])
    constraints = source.list_constraints("SalesDW")
    pk = next(c for c in constraints if c.constraint_type == "PRIMARY_KEY")
    assert pk.columns == ["OrderId", "ProductId"]
    assert pk.table == "OrderDetails"
    assert pk.is_system_named is True


def test_unique_constraint_type_code_maps_to_unique_not_primary_key():
    source = _source([
        ("FROM sys.key_constraints kc", [
            ("dbo", "Customers", "UQ_Customers_Email", "UQ", False, "Email", 1),
        ]),
    ])
    constraints = source.list_constraints("SalesDW")
    assert len(constraints) == 1
    assert constraints[0].constraint_type == "UNIQUE"
    assert constraints[0].is_system_named is False


def test_foreign_key_constraint_captures_referenced_table_columns_and_actions():
    source = _source([
        ("FROM sys.foreign_keys fk", [
            ("dbo", "Orders", "FK_Orders_Customers", "dbo", "Customers", "CustomerId", "CustomerId", 1,
             "CASCADE", "NO_ACTION", False, False, False),
        ]),
    ])
    constraints = source.list_constraints("SalesDW")
    fk = next(c for c in constraints if c.constraint_type == "FOREIGN_KEY")
    assert fk.referenced_table == "dbo.Customers"
    assert fk.columns == ["CustomerId"]
    assert fk.referenced_columns == ["CustomerId"]
    assert fk.delete_action == "CASCADE"
    assert fk.update_action == "NO_ACTION"
    assert fk.is_trusted is True  # is_not_trusted was False
    assert fk.is_disabled is False


def test_foreign_key_is_not_trusted_flag_is_inverted_to_is_trusted():
    source = _source([
        ("FROM sys.foreign_keys fk", [
            ("dbo", "Orders", "FK_Orders_Customers", "dbo", "Customers", "CustomerId", "CustomerId", 1,
             "NO_ACTION", "NO_ACTION", True, False, False),
        ]),
    ])
    constraints = source.list_constraints("SalesDW")
    fk = next(c for c in constraints if c.constraint_type == "FOREIGN_KEY")
    assert fk.is_trusted is False  # is_not_trusted was True


def test_check_constraint_captures_definition_trust_and_disabled_state():
    source = _source([
        ("FROM sys.check_constraints cc", [
            ("dbo", "Orders", "CK_Orders_TotalDue", "([TotalDue]>=(0))", False, False, False, "TotalDue"),
        ]),
    ])
    constraints = source.list_constraints("SalesDW")
    ck = next(c for c in constraints if c.constraint_type == "CHECK")
    assert ck.definition == "([TotalDue]>=(0))"
    assert ck.columns == ["TotalDue"]
    assert ck.is_trusted is True
    assert ck.is_disabled is False


def test_table_level_check_constraint_has_no_single_column():
    source = _source([
        ("FROM sys.check_constraints cc", [
            ("dbo", "Orders", "CK_Orders_DateRange", "([ShipDate]>=[OrderDate])", False, False, False, None),
        ]),
    ])
    constraints = source.list_constraints("SalesDW")
    ck = next(c for c in constraints if c.constraint_type == "CHECK")
    assert ck.columns == []


def test_default_constraint_captures_definition_and_column():
    source = _source([
        ("FROM sys.default_constraints dc", [
            ("dbo", "Orders", "DF_Orders_ModifiedDate", "(sysutcdatetime())", False, "ModifiedDate"),
        ]),
    ])
    constraints = source.list_constraints("SalesDW")
    df = next(c for c in constraints if c.constraint_type == "DEFAULT")
    assert df.definition == "(sysutcdatetime())"
    assert df.columns == ["ModifiedDate"]


def test_fixture_metadata_source_produces_all_constraint_types():
    source = FixtureMetadataSource(catalog=MockCatalog())
    constraints = source.list_constraints("SalesDW")
    types_present = {c.constraint_type for c in constraints}
    assert types_present == {"PRIMARY_KEY", "FOREIGN_KEY", "CHECK", "DEFAULT"}
    assert any(c.name == "PK_Orders" for c in constraints)
