"""
Step 0 spike: runs sqlglot (lineage) and the XML parser against the
synthetic sample environment in fixtures/, and records concrete
coverage/accuracy numbers used in step0_report.md.

Only sqlglot and XML parsing are exercised here as *executed* spikes --
this sandbox has no Databricks Lakebridge install and no live Anthropic
API key, so those two rows in the report's coverage matrix are annotated
as "not independently executed" rather than measured. See step0_report.md
for how that gap is handled.

Run: python3 spike/spike_runner.py
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from autovista.dtsx_xml_parser import parse_dtsx_file
from autovista.sql_lineage_parser import parse_lineage
from fixtures.mock_catalog import MockCatalog


def spike_sqlglot() -> dict:
    catalog = MockCatalog()
    proc_results = {}
    for key, proc in catalog.procedures.items():
        r = parse_lineage(proc.definition)
        proc_results[key] = {
            "parse_status": r.parse_status,
            "referenced_tables": r.referenced_tables,
            "referenced_procs": r.referenced_procs,
            "unresolved_reason": r.unresolved_reason,
        }

    view_results = {}
    for key, view in catalog.views.items():
        r = parse_lineage(view.definition)
        view_results[key] = {"parse_status": r.parse_status, "referenced_tables": r.referenced_tables}

    resolved = sum(1 for r in proc_results.values() if r["parse_status"] == "sqlglot")
    unresolved = sum(1 for r in proc_results.values() if r["parse_status"] == "unresolved")

    return {
        "total_procedures": len(proc_results),
        "resolved_via_sqlglot": resolved,
        "correctly_flagged_unresolved": unresolved,  # usp_DynamicReportBuilder, by design
        "total_views": len(view_results),
        "views_resolved": sum(1 for r in view_results.values() if r["parse_status"] == "sqlglot"),
        "proc_detail": proc_results,
        "view_detail": view_results,
    }


def spike_xml_parser() -> dict:
    results = {}
    for path in sorted(glob.glob("fixtures/dtsx/*.dtsx")):
        pkg = parse_dtsx_file(path, project="DiscoveryPilot")
        script_tasks = [t for t in pkg.tasks if t.unparseable_body]
        execute_package_edges = [t.executed_package for t in pkg.tasks if t.executed_package]
        embedded_sql_count = len(pkg.embedded_sql)
        results[pkg.name] = {
            "task_count": len(pkg.tasks),
            "connection_managers": len(pkg.connection_managers),
            "variables": len(pkg.variables),
            "precedence_constraints": len(pkg.precedence_constraints),
            "embedded_sql_extracted": embedded_sql_count,
            "script_tasks_flagged_unparseable": len(script_tasks),
            "package_to_package_edges": execute_package_edges,
        }
    return {
        "total_packages": len(results),
        "total_script_tasks_flagged": sum(r["script_tasks_flagged_unparseable"] for r in results.values()),
        "total_execute_package_edges": sum(len(r["package_to_package_edges"]) for r in results.values()),
        "package_detail": results,
    }


if __name__ == "__main__":
    output = {
        "sqlglot_spike": spike_sqlglot(),
        "xml_parser_spike": spike_xml_parser(),
    }
    out_path = Path(__file__).parent / "spike_results.json"
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(json.dumps({
        "sqlglot: procs resolved": f"{output['sqlglot_spike']['resolved_via_sqlglot']}/{output['sqlglot_spike']['total_procedures']}",
        "sqlglot: correctly flagged unresolved": output['sqlglot_spike']['correctly_flagged_unresolved'],
        "sqlglot: views resolved": f"{output['sqlglot_spike']['views_resolved']}/{output['sqlglot_spike']['total_views']}",
        "xml: packages parsed": output['xml_parser_spike']['total_packages'],
        "xml: script tasks flagged": output['xml_parser_spike']['total_script_tasks_flagged'],
        "xml: package->package edges": output['xml_parser_spike']['total_execute_package_edges'],
    }, indent=2))
