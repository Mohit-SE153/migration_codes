"""
Tests for Discovery Enhancement 4: the metadata-driven Data Quality
Summary. Pure-Python -- fed synthetic TableEntity/IndexEntity/
ConstraintEntity objects rather than a live/fixture source, since the
analyzer itself issues no SQL of its own.
"""
from __future__ import annotations

from autovista.data_quality_analyzer import build_data_quality_summary
from autovista.schema import ColumnEntity, ConstraintEntity, IndexEntity, TableEntity


def _table(name, schema="dbo", row_count=100, size_mb=1.0, table_type="CLUSTERED", columns=None, **kwargs):
    return TableEntity(
        database="SalesDW", schema=schema, name=name, row_count=row_count, size_mb=size_mb,
        column_count=len(columns or []), columns=columns or [], table_type=table_type, **kwargs,
    )


def test_empty_tables_and_heap_tables_are_counted():
    tables = [
        _table("EmptyTable", row_count=0),
        _table("HeapTable", row_count=10, table_type="HEAP"),
        _table("NormalTable", row_count=10, table_type="CLUSTERED"),
    ]
    summary = build_data_quality_summary("SalesDW", tables, indexes=[], constraints=[])
    assert summary.total_tables == 3
    assert summary.empty_tables == 1
    assert summary.heap_tables == 1


def test_tables_without_primary_key_foreign_key_or_clustered_index():
    tables = [_table("Orders"), _table("Lookup")]
    constraints = [
        ConstraintEntity(database="SalesDW", schema="dbo", table="Orders", name="PK_Orders", constraint_type="PRIMARY_KEY", columns=["OrderId"]),
        ConstraintEntity(database="SalesDW", schema="dbo", table="Orders", name="FK_Orders_Customers", constraint_type="FOREIGN_KEY", columns=["CustomerId"], referenced_table="dbo.Customers"),
    ]
    indexes = [
        IndexEntity(database="SalesDW", schema="dbo", table="Orders", name="PK_Orders", is_clustered=True),
    ]
    summary = build_data_quality_summary("SalesDW", tables, indexes, constraints)
    assert summary.tables_without_primary_key == 1  # Lookup
    assert summary.tables_without_foreign_key == 1  # Lookup
    assert summary.tables_without_clustered_index == 1  # Lookup


def test_trigger_identity_computed_and_sparse_column_flags():
    tables = [
        _table("WithTrigger", trigger_count=1),
        _table("WithIdentity", identity_columns=["Id"]),
        _table("WithComputed", computed_columns=["FullName"]),
        _table("WithSparse", sparse_columns=["OptionalNote"]),
        _table("PlainTable"),
    ]
    summary = build_data_quality_summary("SalesDW", tables, indexes=[], constraints=[])
    assert summary.tables_with_triggers == 1
    assert summary.tables_with_identity_columns == 1
    assert summary.tables_with_computed_columns == 1
    assert summary.tables_with_sparse_columns == 1


def test_cdc_change_tracking_and_temporal_flags():
    tables = [
        _table("CdcTable", is_cdc_enabled=True),
        _table("TrackedTable", is_change_tracking_enabled=True),
        _table("TemporalTable", is_temporal_table=True),
    ]
    summary = build_data_quality_summary("SalesDW", tables, indexes=[], constraints=[])
    assert summary.tables_with_cdc_enabled == 1
    assert summary.tables_with_change_tracking_enabled == 1
    assert summary.tables_with_temporal_tables == 1


def test_column_level_type_indicators():
    columns = [
        ColumnEntity(name="Id", data_type="int", nullable=False, ordinal_position=1),
        ColumnEntity(name="Notes", data_type="text", nullable=True, ordinal_position=2),
        ColumnEntity(name="Payload", data_type="sql_variant", nullable=True, ordinal_position=3),
        ColumnEntity(name="Location", data_type="geography", nullable=True, ordinal_position=4),
        ColumnEntity(name="Doc", data_type="xml", nullable=True, ordinal_position=5),
        ColumnEntity(name="BigText", data_type="nvarchar", nullable=True, ordinal_position=6, max_length=-1),
        ColumnEntity(name="ShortText", data_type="nvarchar", nullable=True, ordinal_position=7, max_length=100),
        ColumnEntity(name="Custom", data_type="MyClrType", nullable=True, ordinal_position=8, is_clr_type=True),
        ColumnEntity(name="Blob", data_type="varbinary", nullable=True, ordinal_position=9, is_filestream=True, max_length=-1),
    ]
    tables = [_table("Wide", columns=columns)]
    summary = build_data_quality_summary("SalesDW", tables, indexes=[], constraints=[])

    assert summary.non_nullable_columns == 1
    assert summary.nullable_columns == 8
    assert summary.deprecated_data_type_columns == 1  # text
    assert summary.text_ntext_image_columns == 1
    assert summary.sql_variant_columns == 1
    # "BigText" (nvarchar max) + "Blob" (varbinary max) both have max_length == -1
    assert summary.large_max_columns == 2
    assert summary.tables_with_xml_columns == 1
    assert summary.tables_with_spatial_columns == 1
    assert summary.tables_with_clr_types == 1
    assert summary.tables_with_filestream == 1
    assert summary.tables_with_lob_columns == 1  # text/nvarchar/varbinary/xml all count as LOB-eligible


def test_duplicate_column_names_across_tables_are_counted():
    tables = [
        _table("Orders", columns=[ColumnEntity(name="CreatedDate", data_type="datetime2", nullable=False, ordinal_position=1)]),
        _table("Invoices", columns=[ColumnEntity(name="CreatedDate", data_type="datetime2", nullable=False, ordinal_position=1)]),
        _table("Products", columns=[ColumnEntity(name="Sku", data_type="varchar", nullable=False, ordinal_position=1)]),
    ]
    summary = build_data_quality_summary("SalesDW", tables, indexes=[], constraints=[])
    assert summary.duplicate_column_names == 1  # "CreatedDate" appears twice


def test_largest_tables_wide_schema_and_excessive_index_lists():
    wide_columns = [ColumnEntity(name=f"col{i}", data_type="int", nullable=True, ordinal_position=i) for i in range(60)]
    tables = [
        _table("Big", size_mb=500.0),
        _table("Small", size_mb=1.0),
        _table("Wide", size_mb=2.0, columns=wide_columns),
    ]
    indexes = [
        IndexEntity(database="SalesDW", schema="dbo", table="Small", name=f"IX_{i}")
        for i in range(12)
    ]
    summary = build_data_quality_summary("SalesDW", tables, indexes, constraints=[])
    assert summary.largest_tables[0] == "dbo.Big"
    assert "dbo.Wide" in summary.wide_schema_tables
    assert "dbo.Small" in summary.excessive_index_tables


def test_average_row_length_is_derived_from_size_and_row_count():
    tables = [_table("Orders", row_count=1000, size_mb=1.0)]  # 1 MB / 1000 rows
    summary = build_data_quality_summary("SalesDW", tables, indexes=[], constraints=[])
    expected = round(1.0 * 1024 * 1024 / 1000, 2)
    assert summary.average_row_length_bytes == expected


def test_empty_table_list_returns_zeroed_summary_without_crashing():
    summary = build_data_quality_summary("SalesDW", tables=[], indexes=[], constraints=[])
    assert summary.total_tables == 0
    assert summary.average_row_length_bytes is None
    assert summary.largest_tables == []
