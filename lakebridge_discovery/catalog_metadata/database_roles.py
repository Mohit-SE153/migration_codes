"""
Database-level role inventory discovery, from SQL Server catalog metadata
only (sys.database_principals) -- no SQL parsing, no dependence on the
Analyzer report. Sibling probe to database_users.py -- see that module's
docstring for why these are two separate probes/result fields instead of
one, and why both reuse ServerPrincipalEntity.

Pure object-inventory discovery -- appends to result.database_roles, never
result.server_principals or result.database_users.
"""
from __future__ import annotations

from lakebridge_discovery.schema import LakebridgeDiscoveryResult, ServerPrincipalEntity

NAME = "database_roles"

_QUERY_DATABASE_ROLES = """
SELECT dp.name, dp.is_fixed_role
FROM sys.database_principals dp
WHERE dp.type = 'R'
ORDER BY dp.name
"""


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_DATABASE_ROLES)

    seen_names: set[str] = set()
    for name, is_fixed_role in cursor.fetchall():
        if name in seen_names:
            continue
        seen_names.add(name)
        result.database_roles.append(ServerPrincipalEntity(
            name=name,
            principal_type="ROLE",
            is_fixed_role=bool(is_fixed_role),
        ))
