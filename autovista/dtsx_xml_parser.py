"""
Parses raw .dtsx (SSIS package XML) into structured control-flow /
data-flow / connection-manager / variable metadata.

This module's job stops at the XML boundary: it extracts task structure,
connection managers, variables, precedence constraints, and embedded SQL
TEXT verbatim. It does NOT parse the SQL itself for lineage (that's
sql_lineage_parser's job) and it does NOT interpret Script Task source
code (that's out of scope for any static parser -- flagged
`unparseable_body=True` for the LLM fallback / human review instead).

Works on both file-deployed .dtsx files and XML pulled from the SSISDB
catalog (`catalog.get_project`/package blobs) -- same parser, the caller
just hands it bytes/text either way.
"""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from autovista.schema import (
    ConnectionManagerEntity,
    EmbeddedSqlEntity,
    PackageEntity,
    PackageVariableEntity,
    PrecedenceConstraintEntity,
    SsisTaskEntity,
)

NS = {
    "DTS": "www.microsoft.com/SqlServer/Dts",
    "SQLTask": "www.microsoft.com/sqlserver/dts/tasks/sqltask",
    "ScriptTask": "www.microsoft.com/sqlserver/dts/tasks/scripttask",
    "ExecutePackageTask": "www.microsoft.com/SqlServer/Dts/Tasks/ExecutePackageTask",
}

TASK_TYPE_LABELS = {
    "Microsoft.ExecuteSQLTask": "Execute SQL Task",
    "Microsoft.ScriptTask": "Script Task",
    "Microsoft.ExecutePackageTask": "Execute Package Task",
    "Microsoft.FileSystemTask": "File System Task",
    "STOCK:PipelineTask": "Data Flow Task",
    "STOCK:SEQUENCE": "Sequence Container",
    "STOCK:FOREACHLOOP": "ForEach Loop Container",
}

CONTAINER_TYPES = {"STOCK:SEQUENCE", "STOCK:FOREACHLOOP"}

_CONN_STRING_SECRET_PATTERN = re.compile(r"(?i)(password|pwd)\s*=\s*[^;]*")


def _redact_connection_string(conn_str: str) -> str:
    return _CONN_STRING_SECRET_PATTERN.sub(r"\1=***REDACTED***", conn_str)


def _q(tag: str) -> str:
    """Qualify a `DTS:Tag` shorthand into ElementTree's {uri}Tag form."""
    prefix, _, local = tag.partition(":")
    return f"{{{NS[prefix]}}}{local}"


def _object_name(executable: ET.Element, fallback: str) -> str:
    for prop in executable.findall(_q("DTS:Property")):
        if prop.get(_q("DTS:Name")) == "ObjectName":
            return prop.text or fallback
    return fallback


def _extract_embedded_sql_from_task(executable: ET.Element, task_name: str, task_type: str) -> list[EmbeddedSqlEntity]:
    results: list[EmbeddedSqlEntity] = []

    sql_task_data = executable.find(f".//{_q('SQLTask:SqlTaskData')}")
    if sql_task_data is not None:
        sql_text = sql_task_data.get(_q("SQLTask:SqlStatementSource"))
        if sql_text:
            results.append(
                EmbeddedSqlEntity(task_name=task_name, task_type=task_type, sql_text=sql_text.strip())
            )

    # Data flow components (OLE DB Source/Destination, Lookup, OLE DB
    # Command) carry SQL in a `SqlCommand` property under <pipeline>.
    for component in executable.findall(".//component"):
        comp_name = component.get("name", "unnamed component")
        for prop in component.findall("./properties/property"):
            if prop.get("name") == "SqlCommand" and (prop.text or "").strip():
                results.append(
                    EmbeddedSqlEntity(
                        task_name=f"{task_name} :: {comp_name}",
                        task_type=component.get("componentClassID", "DataFlowComponent"),
                        sql_text=prop.text.strip(),
                    )
                )

    return results


def _parse_executable(
    executable: ET.Element,
    parent_container: str | None,
    tasks: list[SsisTaskEntity],
    all_embedded_sql: list[EmbeddedSqlEntity],
) -> None:
    exec_type = executable.get(_q("DTS:ExecutableType"), "Unknown")
    name = _object_name(executable, fallback=f"unnamed_{exec_type}")
    label = TASK_TYPE_LABELS.get(exec_type, exec_type)

    if exec_type in CONTAINER_TYPES:
        tasks.append(SsisTaskEntity(name=name, task_type=label, parent_container=parent_container))
        child_container = executable.find(_q("DTS:Executables"))
        if child_container is not None:
            for child in child_container.findall(_q("DTS:Executable")):
                _parse_executable(child, parent_container=name, tasks=tasks, all_embedded_sql=all_embedded_sql)
        return

    embedded_sql = _extract_embedded_sql_from_task(executable, name, label)
    all_embedded_sql.extend(embedded_sql)

    executed_package = None
    if exec_type == "Microsoft.ExecutePackageTask":
        ept_data = executable.find(f".//{_q('ExecutePackageTask:ExecutePackageTaskData')}")
        if ept_data is not None:
            raw = ept_data.get(_q("ExecutePackageTask:PackageName"))
            executed_package = raw[:-5] if raw and raw.lower().endswith(".dtsx") else raw

    unparseable_body = False
    if exec_type == "Microsoft.ScriptTask":
        # Script Task source code is opaque to any static parser here --
        # explicitly flagged rather than silently skipped, so it surfaces
        # for LLM-assisted extraction / human review downstream.
        unparseable_body = executable.find(f".//{_q('ScriptTask:ScriptTaskData')}") is not None

    tasks.append(
        SsisTaskEntity(
            name=name,
            task_type=label,
            parent_container=parent_container,
            embedded_sql=embedded_sql,
            executed_package=executed_package,
            unparseable_body=unparseable_body,
        )
    )


def parse_dtsx(xml_text: str, package_name_hint: str, project: str, deployment_model: str = "ssisdb") -> PackageEntity:
    root = ET.fromstring(xml_text)

    package_name = _object_name(root, fallback=package_name_hint)

    connection_managers = []
    cm_container = root.find(_q("DTS:ConnectionManagers"))
    if cm_container is not None:
        for cm in cm_container.findall(_q("DTS:ConnectionManager")):
            cm_name = cm.get(_q("DTS:ObjectName"), "unnamed")
            creation_name = cm.get(_q("DTS:CreationName"), "unknown")
            conn_str_el = cm.find(f".//{_q('DTS:ConnectionManager')}")
            raw_conn_str = conn_str_el.get("ConnectionString", "") if conn_str_el is not None else ""
            connection_managers.append(
                ConnectionManagerEntity(
                    name=cm_name, creation_name=creation_name,
                    connection_string_redacted=_redact_connection_string(raw_conn_str),
                )
            )

    variables = []
    var_container = root.find(_q("DTS:Variables"))
    if var_container is not None:
        for var in var_container.findall(_q("DTS:Variable")):
            var_name = var.get(_q("DTS:ObjectName"), "unnamed")
            namespace = var.get(_q("DTS:Namespace"), "User")
            value_el = var.find(_q("DTS:VariableValue"))
            data_type = value_el.get(_q("DTS:DataType"), "unknown") if value_el is not None else "unknown"
            variables.append(PackageVariableEntity(name=var_name, namespace=namespace, data_type=data_type))

    tasks: list[SsisTaskEntity] = []
    all_embedded_sql: list[EmbeddedSqlEntity] = []
    executables_container = root.find(_q("DTS:Executables"))
    if executables_container is not None:
        for executable in executables_container.findall(_q("DTS:Executable")):
            _parse_executable(executable, parent_container=None, tasks=tasks, all_embedded_sql=all_embedded_sql)

    precedence_constraints = []
    pc_container = root.find(_q("DTS:PrecedenceConstraints"))
    if pc_container is not None:
        for pc in pc_container.findall(_q("DTS:PrecedenceConstraint")):
            from_ref = pc.get(_q("DTS:From"), "")
            to_ref = pc.get(_q("DTS:To"), "")
            precedence_constraints.append(
                PrecedenceConstraintEntity(
                    from_task=from_ref.rsplit("\\", 1)[-1],
                    to_task=to_ref.rsplit("\\", 1)[-1],
                    evaluation_value=pc.get(_q("DTS:Value"), "Success"),
                )
            )

    return PackageEntity(
        name=package_name,
        project=project,
        deployment_model=deployment_model,  # type: ignore[arg-type]
        tasks=tasks,
        connection_managers=connection_managers,
        variables=variables,
        precedence_constraints=precedence_constraints,
        embedded_sql=all_embedded_sql,
        parse_status="xml_parsed",
    )


def parse_dtsx_file(path: str, project: str, deployment_model: str = "file_system") -> PackageEntity:
    with open(path, "r", encoding="utf-8") as f:
        xml_text = f.read()
    package_name_hint = path.rsplit("/", 1)[-1].removesuffix(".dtsx")
    return parse_dtsx(xml_text, package_name_hint=package_name_hint, project=project, deployment_model=deployment_model)
