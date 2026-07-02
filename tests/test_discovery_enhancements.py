from autovista.sql_metadata_extractor import FixtureMetadataSource, extract_database_metadata
from fixtures.mock_catalog import MockCatalog


def test_fixture_metadata_includes_extended_database_and_table_fields():
    source = FixtureMetadataSource(catalog=MockCatalog())
    result, _ = extract_database_metadata(source, database="SalesDW")

    db = result["databases"][0]
    assert db.recovery_model == "FULL"
    assert db.compatibility_level == "SQL Server 2022"
    assert db.database_owner == "dbo"
    assert db.data_file_size_mb > 0
    assert db.log_file_size_mb > 0

    table = next(t for t in result["tables"] if t.name == "Orders")
    assert table.create_date is not None
    assert table.modify_date is not None
    assert table.index_count >= 0
    assert table.foreign_key_count >= 0
    assert table.partition_count >= 0

    column = next(c for c in table.columns if c.name == "OrderId")
    assert column.is_part_of_pk is True
    assert column.is_nullable is False
    assert column.identity_seed is not None


def test_fixture_metadata_includes_indexes_functions_synonyms_and_security_lists():
    source = FixtureMetadataSource(catalog=MockCatalog())
    result, _ = extract_database_metadata(source, database="SalesDW")

    assert result["indexes"]
    assert result["functions"]
    assert result["synonyms"]
    assert result["sequences"]
    assert result["user_defined_types"]
    assert result["xml_schema_collections"]
    assert result["assemblies"]
    assert result["security_principals"]
    assert result["permissions"]
    assert result["database_summary"]
