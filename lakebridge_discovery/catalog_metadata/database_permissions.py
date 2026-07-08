"""
Database-level permission inventory discovery, from SQL Server catalog
metadata only (sys.database_permissions / sys.database_principals) -- no
SQL parsing, no dependence on the Analyzer report.

Pure object-inventory discovery -- appends to result.database_permissions,
reusing ServerPermissionEntity (identical field shape to this package's
existing server-scoped server_permissions -- see source_exporter.py -- a
database-level GRANT/DENY/REVOKE row is the same kind of catalog fact, just
database- rather than server-scoped). Never touches result.server_permissions.

Retyped independently of autovista.sql_metadata_extractor's QUERY_PERMISSIONS
(same catalog views, same column set), not shared code.
"""
from __future__ import annotations

from lakebridge_discovery.schema import LakebridgeDiscoveryResult, ServerPermissionEntity

NAME = "database_permissions"

_QUERY_DATABASE_PERMISSIONS = """
SELECT dp.name AS grantee_name, dp.type AS principal_type, perm.class_desc,
       OBJECT_NAME(perm.major_id) AS object_name, perm.permission_name, perm.state_desc
FROM sys.database_permissions perm
JOIN sys.database_principals dp ON dp.principal_id = perm.grantee_principal_id
ORDER BY dp.name, perm.permission_name
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_DATABASE_PERMISSIONS)

    for grantee_name, principal_type, class_desc, object_name, permission_name, state_desc in cursor.fetchall():
        result.database_permissions.append(ServerPermissionEntity(
            grantee=grantee_name,
            principal_type=principal_type,
            class_desc=class_desc,
            object_name=object_name,
            permission_name=permission_name,
            state_desc=state_desc,
        ))
