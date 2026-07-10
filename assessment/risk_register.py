"""
Object-level migration risk register.

Three independent, additive sources feed this list -- each is read
directly from data Discovery already computed, never re-derived by a
second parsing pass:

  1. manifest["unsupported_objects"] -- Discovery's own authoritative
     "sqlglot/lineage parsing gave up on this object" signal (see
     autovista/schema.py's UnsupportedObjectEntity docstring). Used
     verbatim, not re-derived from parse_status here.
  2. compatibility_flags already attached to stored procs/views/functions/
     triggers/embedded SQL by autovista/compatibility_scanner.py -- mapped
     to a severity + a migration note via _FLAG_INFO below (this
     assessment's own judgment call on Databricks/Spark SQL equivalence,
     not sourced from the scanner itself, which only detects presence).
  3. CLR assemblies and linked servers -- both have no Databricks
     equivalent at all and are therefore always risk-register material,
     regardless of whether anything currently references them.

Table/schema-level DDL feature risk (temporal tables, CDC, heap tables,
deprecated data types, ...) is intentionally rolled up in
data_readiness.py instead, as aggregate counts -- keeping this register
scoped to specific, individually-named objects with specific problems
avoids two modules re-describing the same tables/counts two different
ways.
"""
from __future__ import annotations

from assessment.schema import RiskFinding

_SEVERITY_RANK = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}

# flag -> (severity, migration note). Severity reflects how far the
# construct is from a direct Databricks/Spark SQL / Delta Lake equivalent,
# not how "bad" the SQL is -- see module docstring.
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


def _compat_flag_findings(objects: list[dict], object_type: str, name_field: str = "name") -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    for obj in objects:
        flags = obj.get("compatibility_flags") or []
        if not flags:
            continue
        schema = obj.get("schema")
        name = f"{schema}.{obj[name_field]}" if schema else obj.get(name_field, obj.get("task_name", "(unnamed)"))
        remediation = " | ".join(f"{f}: {_FLAG_INFO.get(f, ('Medium', 'Review manually -- no mapping on file.'))[1]}" for f in sorted(flags))
        if obj.get("compatibility_notes"):
            remediation += f" | Reviewer note: {obj['compatibility_notes']}"
        findings.append(RiskFinding(
            object_type=object_type, name=name, category="COMPATIBILITY_FLAGS",
            severity=_worst_severity(flags),
            description=f"Uses SQL Server feature(s) with limited/no direct Databricks equivalent: {', '.join(sorted(flags))}",
            remediation=remediation,
        ))
    return findings


def _unsupported_object_findings(unsupported_objects: list[dict]) -> list[RiskFinding]:
    """Discovery can emit more than one identical row for the same object
    name -- e.g. a single CREATE TRIGGER ... FOR INSERT, UPDATE, DELETE is
    modeled as three separate TriggerEntity rows (one per event) sharing
    one name (see autovista/sql_metadata_extractor.py), so the same
    unresolved reason can appear 2-3 times for what a migration engineer
    would treat as one artifact. Collapsed here into one finding with an
    occurrence count, rather than shown as repeated, indistinguishable
    rows in the risk register."""
    counts: dict[tuple, int] = {}
    order: list[tuple] = []
    for u in unsupported_objects:
        key = (
            u.get("object_type", "unknown"), u.get("name", "(unnamed)"),
            u.get("parse_status") == "unresolved",
            u.get("reason") or "sqlglot could not fully resolve this object.",
        )
        if key not in counts:
            order.append(key)
        counts[key] = counts.get(key, 0) + 1

    findings: list[RiskFinding] = []
    for object_type, name, unresolved, reason in order:
        occurrences = counts[(object_type, name, unresolved, reason)]
        remediation = "Manually review this object's definition and confirm its true table/proc dependencies before trusting the dependency graph for it."
        if occurrences > 1:
            remediation += (
                f" (Discovery recorded this same finding {occurrences} times for '{name}' -- likely one "
                f"definition registered per trigger event; treat as a single migration artifact.)"
            )
        findings.append(RiskFinding(
            object_type=object_type, name=name,
            category="PARSE_UNRESOLVED" if unresolved else "PARSE_PARTIAL",
            severity="High" if unresolved else "Medium",
            description=reason, remediation=remediation,
        ))
    return findings


def build_risk_register(manifest: dict) -> list[RiskFinding]:
    findings: list[RiskFinding] = _unsupported_object_findings(manifest.get("unsupported_objects", []))

    for object_type, field_name in (
        ("stored_procedure", "stored_procedures"), ("view", "views"),
        ("function", "functions"), ("trigger", "triggers"),
    ):
        findings.extend(_compat_flag_findings(manifest.get(field_name, []), object_type))

    for package in manifest.get("packages", []):
        for task in package.get("tasks", []):
            findings.extend(_compat_flag_findings(task.get("embedded_sql", []) or [], "ssis_embedded_sql", name_field="task_name"))
        findings.extend(_compat_flag_findings(package.get("embedded_sql", []) or [], "ssis_embedded_sql", name_field="task_name"))

    for assembly in manifest.get("assemblies", []):
        permission_set = assembly.get("permission_set") or ""
        name = f"{assembly.get('schema')}.{assembly['name']}" if assembly.get("schema") else assembly.get("name", "(unnamed)")
        findings.append(RiskFinding(
            object_type="clr_assembly", name=name, category="CLR_ASSEMBLY",
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
