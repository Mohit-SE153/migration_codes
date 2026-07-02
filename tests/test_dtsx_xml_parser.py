from autovista.dtsx_xml_parser import parse_dtsx_file


def test_parses_master_package_execute_package_tasks():
    pkg = parse_dtsx_file("fixtures/dtsx/Pkg_Master.dtsx", project="DiscoveryPilot")
    executed = {t.executed_package for t in pkg.tasks if t.executed_package}
    assert executed == {"Pkg_LoadCustomers", "Pkg_LoadOrders", "Pkg_LoadOrderDetails", "Pkg_ArchiveOldData"}


def test_flags_script_task_as_unparseable():
    pkg = parse_dtsx_file("fixtures/dtsx/Pkg_LoadOrderDetails.dtsx", project="DiscoveryPilot")
    script_tasks = [t for t in pkg.tasks if t.task_type == "Script Task"]
    assert len(script_tasks) == 1
    assert script_tasks[0].unparseable_body is True


def test_extracts_container_nesting():
    pkg = parse_dtsx_file("fixtures/dtsx/Pkg_LoadOrders.dtsx", project="DiscoveryPilot")
    sequence = next(t for t in pkg.tasks if t.task_type == "Sequence Container")
    children = [t for t in pkg.tasks if t.parent_container == sequence.name]
    assert len(children) == 2


def test_redacts_connection_string_secrets():
    xml = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ExecutableType="Microsoft.Package">
  <DTS:Property DTS:Name="ObjectName">Pkg_Test</DTS:Property>
  <DTS:ConnectionManagers>
    <DTS:ConnectionManager DTS:refId="x" DTS:CreationName="OLEDB" DTS:ObjectName="Conn">
      <DTS:ObjectData>
        <DTS:ConnectionManager ConnectionString="Data Source=x;User ID=svc;Password=Sup3rSecret;" />
      </DTS:ObjectData>
    </DTS:ConnectionManager>
  </DTS:ConnectionManagers>
  <DTS:Variables/>
  <DTS:Executables/>
</DTS:Executable>"""
    from autovista.dtsx_xml_parser import parse_dtsx
    pkg = parse_dtsx(xml, package_name_hint="Pkg_Test", project="Test")
    assert "Sup3rSecret" not in pkg.connection_managers[0].connection_string_redacted
    assert "REDACTED" in pkg.connection_managers[0].connection_string_redacted
