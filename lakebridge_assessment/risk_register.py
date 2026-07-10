"""
Object-level migration risk register, built from lakebridge_manifest.json.
Independent reimplementation of assessment/risk_register.py's approach
(same flag-severity judgment calls, not imported -- see package docstring
in schema.py) adapted to Lakebridge's flatter LakebridgeObjectRef shape:
no per-object parse_status/unresolved_reason fields to read, so
unsupported_objects entries are reported using whatever `notes` text the
Analyzer report actually carried, without inventing a reason string sqlglot
would have (Lakebridge's own report just doesn't carry one).
"""
from __future__ import annotations

from lakebridge_assessment.schema import RiskFinding

_SEVERITY_RANK = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}

_FLAG_INFO: dict[str, tuple[str, str]] = {
    "MERGE": ("Medium", "Delta Lake supports MERGE INTO directly; syntax differs slightly, semantics map well."),
    "PIVOT": ("Low", "Databricks SQL supports PIVOT natively; verify syntax parity during conversion."),
    "UNPIVOT": ("Low", "Databricks SQL supports UNPIVOT natively; verify syntax parity during conversion."),
    "CROSS_APPLY": ("Medium", "No CROSS APPLY keyword; typically rewritten as a LATERAL join or LATERAL VIEW depending on the correlated subquery shape."),
    "OUTER_APPLY": ("Medium", "No OUTER APPLY keyword; typically rewritten as a LEFT JOIN LATERAL."),
    "OPENJSON": ("Medium", "No OPENJSON equivalent; rewrite using from_json/variant functions against the JSON string."),
    "FOR_XML": ("High", "No FOR XML equivalent; requires restructuring the query (struct/array + to_xml UDF) or moving XML generation downstream."),
    "FOR_JSON": ("Medium", "No FOR JSON clause; rewrite using to_json/struct/named_struct."),
    "OPENQUERY": ("High", "Ad hoc remote query against another server; needs Lakehouse Federation or a dedicated ingestion pipeline."),
    "OPENDATASOURCE": ("High", "Ad hoc remote connection; needs Lakehouse Federation or a dedicated ingestion pipeline."),
    "XP_CMDSHELL": ("Critical", "OS-level shell execution; no equivalent and a governance/security concern -- must be re-architected outside the SQL layer."),
    "SP_OA": ("Critical", "OLE Automation stored procedures; no equivalent at all, must be rewritten as external code (e.g. a Python job)."),
    "LINKED_SERVER": ("Critical", "Cross-server reference; needs Lakehouse Federation, an ingestion pipeline, or Unity Catalog external tables."),
}


def _worst_severity(flags: list[str]) -> str:
    return max((_FLAG_INFO.get(f, ("Medium", ""))[0] for f in flags), key=lambda s: _SEVERITY_RANK[s], default="Low")


def _parse_notes_kv(notes: str | None) -> dict[str, str]:
    """Lakebridge's assemblies/other rows pack extra facts into a single
    `notes` string as `key=value;key=value` (see
    lakebridge_discovery/catalog_metadata's assembly probe) -- there's no
    structured field for this in LakebridgeObjectRef."""
    if not notes:
        return {}
    pairs = {}
    for part in notes.split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            pairs[key.strip()] = value.strip()
    return pairs


def _compat_flag_findings(objects: list[dict], object_type: str) -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    for obj in objects:
        flags = obj.get("compatibility_flags") or []
        if not flags:
            continue
        remediation = " | ".join(f"{f}: {_FLAG_INFO.get(f, ('Medium', 'Review manually -- no mapping on file.'))[1]}" for f in sorted(flags))
        if obj.get("compatibility_notes"):
            remediation += f" | Reviewer note: {obj['compatibility_notes']}"
        findings.append(RiskFinding(
            object_type=object_type, name=obj.get("name", "(unnamed)"), category="COMPATIBILITY_FLAGS",
            severity=_worst_severity(flags),
            description=f"Uses SQL Server feature(s) with limited/no direct Databricks equivalent: {', '.join(sorted(flags))}",
            remediation=remediation,
        ))
    return findings


def build_risk_register(manifest: dict) -> list[RiskFinding]:
    findings: list[RiskFinding] = []

    for u in manifest.get("unsupported_objects", []):
        findings.append(RiskFinding(
            object_type=u.get("object_type", "unknown"), name=u.get("name", "(unnamed)"),
            category="LAKEBRIDGE_UNSUPPORTED", severity="High",
            description=u.get("notes") or "Flagged as unsupported by the Lakebridge Analyzer report (no further detail available in the report).",
            remediation="Manually review this object -- the Analyzer report doesn't provide a specific reason, unlike sqlglot's parse-error detail.",
        ))

    for object_type, field_name in (
        ("table", "tables"), ("view", "views"), ("stored_procedure", "stored_procedures"),
        ("function", "functions"), ("trigger", "triggers"),
    ):
        findings.extend(_compat_flag_findings(manifest.get(field_name, []), object_type))

    for assembly in manifest.get("assemblies", []):
        notes_kv = _parse_notes_kv(assembly.get("notes"))
        permission_set = notes_kv.get("permission_set", "")
        findings.append(RiskFinding(
            object_type="clr_assembly", name=assembly.get("name", "(unnamed)"), category="CLR_ASSEMBLY",
            severity="Critical" if permission_set in ("UNSAFE_ACCESS", "EXTERNAL_ACCESS") else "High",
            description=f"CLR assembly (permission_set={permission_set or 'unknown'}); Databricks has no CLR/.NET assembly hosting.",
            remediation="Rewrite any CLR UDFs/UDTs/procs backed by this assembly as native Python/Scala/SQL UDFs before migration.",
        ))

    for linked_server in manifest.get("linked_servers", []):
        findings.append(RiskFinding(
            object_type="linked_server", name=linked_server.get("name", "(unnamed)"), category="LINKED_SERVER",
            severity="Critical",
            description=f"Linked server to product={linked_server.get('product') or 'unknown'}; queries against it have no direct Databricks equivalent.",
            remediation="Replace with Lakehouse Federation, a dedicated ingestion pipeline, or refactor consuming queries to pull from a migrated/ingested copy of the data.",
        ))

    return findings
