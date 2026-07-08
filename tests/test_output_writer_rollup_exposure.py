"""
Tests for autovista.output_writer's rollup exposure of already-discovered
categories that previously had no discovery_rollup.csv row: server_instance,
trigger, user_defined_type, xml_schema_collection, agent_job, clr_assembly,
linked_server, database_summary, data_quality_summary, and the
security_principals/permissions split into database_user/database_role/
server_principal/database_permission/server_permission.

Every row here is computed from a manifest field the orchestrator already
populates -- no new discovery logic, no new queries. These tests confirm
the split by scope/principal_type is correct, not that the underlying
discovery is correct (that's covered by sql_metadata_extractor's own tests).
"""
from __future__ import annotations

import csv
import tempfile

from autovista.output_writer import write_csv_rollup
from autovista.schema import (
    AgentJobEntity,
    AssemblyEntity,
    DatabaseSummaryEntity,
    DataQualitySummaryEntity,
    DiscoveryManifest,
    LinkedServerEntity,
    PermissionEntity,
    SchemaEntity,
    SecurityPrincipalEntity,
    ServerInstanceEntity,
    TriggerEntity,
    UserDefinedTypeEntity,
    XmlSchemaCollectionEntity,
)


def _manifest_with_all_categories() -> DiscoveryManifest:
    manifest = DiscoveryManifest()
    manifest.server_instance = ServerInstanceEntity(product_version="16.0.4255.1")
    manifest.triggers = [TriggerEntity(database="SalesDW", schema="dbo", name="trgA", table="Orders", event="UPDATE")]
    manifest.user_defined_types = [UserDefinedTypeEntity(database="SalesDW", schema="dbo", name="Flag", type_kind="ALIAS")]
    manifest.xml_schema_collections = [XmlSchemaCollectionEntity(database="SalesDW", schema="dbo", name="Coll")]
    manifest.agent_jobs = [AgentJobEntity(name="NightlyETL", enabled=True)]
    manifest.assemblies = [AssemblyEntity(database="SalesDW", schema="dbo", name="MyAssembly")]
    manifest.linked_servers = [LinkedServerEntity(name="REMOTE1")]
    manifest.database_summary = [DatabaseSummaryEntity(database="SalesDW")]
    manifest.data_quality_summary = [DataQualitySummaryEntity(database="SalesDW")]
    manifest.security_principals = [
        SecurityPrincipalEntity(database="SalesDW", name="dbo", principal_type="USER", scope="database"),
        SecurityPrincipalEntity(database="SalesDW", name="guest", principal_type="USER", scope="database"),
        SecurityPrincipalEntity(database="SalesDW", name="db_owner", principal_type="ROLE", scope="database"),
        SecurityPrincipalEntity(database="", name="sa", principal_type="LOGIN", scope="server"),
    ]
    manifest.permissions = [
        PermissionEntity(database="SalesDW", grantee="dbo", principal_type="S", scope="database"),
        PermissionEntity(database="SalesDW", grantee="public", principal_type="R", scope="database"),
        PermissionEntity(database="", grantee="sa", principal_type="S", scope="server"),
    ]
    return manifest


def _rollup_rows(manifest: DiscoveryManifest) -> dict[str, int]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        rollup_path = write_csv_rollup(manifest, tmp_dir)
        with open(rollup_path, newline="", encoding="utf-8") as f:
            return {row["object_type"]: int(row["count"]) for row in csv.DictReader(f)}


def test_previously_missing_categories_now_have_rollup_rows():
    rows = _rollup_rows(_manifest_with_all_categories())
    assert rows["server_instance"] == 1
    assert rows["trigger"] == 1
    assert rows["user_defined_type"] == 1
    assert rows["xml_schema_collection"] == 1
    assert rows["agent_job"] == 1
    assert rows["clr_assembly"] == 1
    assert rows["linked_server"] == 1
    assert rows["database_summary"] == 1
    assert rows["data_quality_summary"] == 1


def test_security_principals_split_by_scope_and_principal_type():
    rows = _rollup_rows(_manifest_with_all_categories())
    assert rows["database_user"] == 2  # dbo, guest
    assert rows["database_role"] == 1  # db_owner
    assert rows["server_principal"] == 1  # sa


def test_permissions_split_by_scope():
    rows = _rollup_rows(_manifest_with_all_categories())
    assert rows["database_permission"] == 2
    assert rows["server_permission"] == 1


def test_server_instance_reports_zero_when_none():
    manifest = DiscoveryManifest()
    rows = _rollup_rows(manifest)
    assert rows["server_instance"] == 0


def test_all_new_rows_are_zero_on_an_empty_manifest():
    rows = _rollup_rows(DiscoveryManifest())
    for object_type in (
        "trigger", "user_defined_type", "xml_schema_collection", "agent_job", "clr_assembly",
        "linked_server", "database_summary", "data_quality_summary",
        "database_user", "database_role", "server_principal", "database_permission", "server_permission",
    ):
        assert rows[object_type] == 0, object_type


def test_existing_rows_are_unaffected_by_the_new_additions():
    """Requirement 2: existing counts for already-working parameters must
    not change -- schemas is an arbitrary pre-existing category picked as a
    sentinel check."""
    manifest = _manifest_with_all_categories()
    manifest.schemas = [SchemaEntity(database="SalesDW", name="dbo")]
    rows = _rollup_rows(manifest)
    assert rows["schema"] == 1
