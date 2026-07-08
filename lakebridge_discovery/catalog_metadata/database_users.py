"""
Database-level user inventory discovery, from SQL Server catalog metadata
only (sys.database_principals / sys.database_role_members) -- no SQL
parsing, no dependence on the Analyzer report (it has no security-object
inventory category at all).

Pure object-inventory discovery -- appends to result.database_users,
reusing ServerPrincipalEntity (same field shape as this package's existing
server-scoped server_principals -- see source_exporter.py -- a database
user is the identical kind of catalog fact, just database- rather than
server-scoped; the result field name is the scope discriminator, matching
autovista.schema.SecurityPrincipalEntity's own `scope` attribute
convention). Never touches result.server_principals.

WHERE dp.type IN ('U','S','G') mirrors autovista.sql_metadata_extractor's
QUERY_SECURITY exactly (SQL user / SQL user without login / Windows group)
so the two engines' user counts are directly comparable; sys.database_
principals.type = 'R' (database role) is handled by database_roles.py, a
separate probe, not this one.
"""
from __future__ import annotations

from lakebridge_discovery.schema import LakebridgeDiscoveryResult, ServerPrincipalEntity

NAME = "database_users"

_QUERY_DATABASE_USERS = """
SELECT dp.name, dp.principal_id
FROM sys.database_principals dp
WHERE dp.type IN ('U','S','G')
ORDER BY dp.name
"""

_QUERY_ROLE_MEMBERSHIP = """
SELECT drm.member_principal_id, rp.name AS role_name
FROM sys.database_role_members drm
JOIN sys.database_principals rp ON rp.principal_id = drm.role_principal_id
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_ROLE_MEMBERSHIP)
    roles_by_member: dict[int, list[str]] = {}
    for member_principal_id, role_name in cursor.fetchall():
        roles_by_member.setdefault(member_principal_id, []).append(role_name)

    cursor = connection.cursor()
    cursor.execute(_QUERY_DATABASE_USERS)

    seen_names: set[str] = set()
    for name, principal_id in cursor.fetchall():
        if name in seen_names:
            continue
        seen_names.add(name)
        result.database_users.append(ServerPrincipalEntity(
            name=name,
            principal_type="USER",
            member_of_roles=roles_by_member.get(principal_id, []),
        ))
