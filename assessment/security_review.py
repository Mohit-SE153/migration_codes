"""
Security/permissions migration considerations, rolled up from Discovery's
security_principals/permissions/linked_servers/assemblies (all
direct_metadata -- see autovista/schema.py). Informational for planning
Unity Catalog's principal/grant model; this is a migration-planning aid,
not a security audit -- it does not attempt to find every misconfiguration
in the source estate, only the handful of signals that materially affect
migration planning (privileged server roles, cross-server trust, unsafe
code execution, and rough grant volume).
"""
from __future__ import annotations

from assessment.schema import SecurityNote

_PRIVILEGED_SERVER_ROLES = {
    "sysadmin", "securityadmin", "serveradmin", "setupadmin",
    "processadmin", "diskadmin", "dbcreator", "bulkadmin",
}

# Case-insensitive substring match against permission_name -- broad on
# purpose (catches "CONTROL SERVER", "CONTROL", "ALTER ANY LOGIN", "ALTER
# ANY SERVER ROLE", "IMPERSONATE", "TAKE OWNERSHIP", etc.) since SQL
# Server's permission-name vocabulary is large and this is meant to flag
# "look closer here", not enumerate every high-privilege grant precisely.
_HIGH_PRIVILEGE_PERMISSION_MARKERS = ("control", "impersonate", "alter any", "take ownership")


def build_security_notes(manifest: dict) -> list[SecurityNote]:
    notes: list[SecurityNote] = []

    server_principals = [p for p in manifest.get("security_principals", []) if p.get("scope") == "server"]
    privileged_logins = [
        p for p in server_principals
        if any(role in _PRIVILEGED_SERVER_ROLES for role in (p.get("member_of_roles") or []))
    ]
    if privileged_logins:
        names = ", ".join(sorted(p.get("name", "(unnamed)") for p in privileged_logins))
        notes.append(SecurityNote(
            category="PRIVILEGED_SERVER_LOGINS", count=len(privileged_logins), severity="High",
            description=f"{len(privileged_logins)} server login(s) hold a privileged server role (e.g. sysadmin): {names}",
            recommendation="Do not carry over broad admin access by default -- map each login to a scoped Unity Catalog "
                           "principal (user/service principal/group) and grant only the metastore/catalog admin rights "
                           "actually required post-migration.",
        ))

    if manifest.get("linked_servers"):
        count = len(manifest["linked_servers"])
        notes.append(SecurityNote(
            category="LINKED_SERVER_CREDENTIALS", count=count, severity="Medium",
            description=f"{count} linked server(s) configured, each an implicit cross-server trust/credential relationship.",
            recommendation="Re-provision explicitly (Unity Catalog external connections/service principals or Lakehouse "
                           "Federation credentials) rather than assuming the trust relationship carries over.",
        ))

    unsafe_assemblies = [a for a in manifest.get("assemblies", []) if a.get("permission_set") in ("UNSAFE_ACCESS", "EXTERNAL_ACCESS")]
    if unsafe_assemblies:
        notes.append(SecurityNote(
            category="UNSAFE_CLR_ASSEMBLIES", count=len(unsafe_assemblies), severity="High",
            description=f"{len(unsafe_assemblies)} CLR assembly(ies) registered with elevated code-execution permission "
                        f"(UNSAFE_ACCESS/EXTERNAL_ACCESS).",
            recommendation="Review what each assembly actually does before rewriting -- elevated CLR permission often "
                           "means file/network/OS access that needs its own governance decision on Databricks, not just "
                           "a functional rewrite.",
        ))

    permissions = manifest.get("permissions", [])
    high_priv_grants = [
        p for p in permissions
        if any(marker in (p.get("permission_name") or "").lower() for marker in _HIGH_PRIVILEGE_PERMISSION_MARKERS)
    ]
    if high_priv_grants:
        notes.append(SecurityNote(
            category="HIGH_PRIVILEGE_GRANTS", count=len(high_priv_grants), severity="Medium",
            description=f"{len(high_priv_grants)} grant(s) of a high-privilege permission (CONTROL/IMPERSONATE/ALTER ANY/... pattern).",
            recommendation="Review each grantee individually -- Unity Catalog's grant model (catalog/schema/table-scoped "
                           "GRANTs) doesn't map 1:1 to SQL Server's permission set, so these need deliberate re-design, "
                           "not an automatic port.",
        ))

    if permissions:
        distinct_grantees = {p.get("grantee") for p in permissions if p.get("grantee")}
        notes.append(SecurityNote(
            category="PERMISSION_VOLUME", count=len(permissions), severity="Low",
            description=f"{len(permissions)} total permission grant(s) across {len(distinct_grantees)} distinct grantee(s).",
            recommendation="Use this as a rough sizing signal for the identity/grants migration effort -- plan to "
                           "consolidate into Unity Catalog groups rather than re-creating every individual grant 1:1.",
        ))

    return notes
