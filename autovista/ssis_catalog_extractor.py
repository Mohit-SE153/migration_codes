"""
SSISDB catalog metadata: which packages/projects/folders exist, and
retrieval of each package's raw XML for dtsx_xml_parser to walk.

Prefers the SSISDB catalog (`SSISDB.catalog.packages`, `SSISDB.catalog.projects`) when
packages are deployed there -- this is the primary path for this build
(target environment: on-prem SQL Server + SSISDB, per build config).
Falls back to scanning a file-system directory of raw .dtsx files when a
project isn't in the catalog (legacy package-deployment model).

Either way, the package body bytes end up handed to
dtsx_xml_parser.parse_dtsx -- this module's only extra job over a plain
file scan is resolving catalog-level facts (project/folder membership,
deployment metadata) that don't exist in the .dtsx XML itself.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from autovista.dtsx_xml_parser import parse_dtsx, parse_dtsx_file
from autovista.logging_setup import log_object_result, logger
from autovista.schema import PackageEntity

# sys.databases is a server-wide catalog view, queryable from any database
# context, so this check doesn't require switching into SSISDB first --
# unlike querying SSISDB.catalog.projects directly, which raises "Invalid
# object name" with a confusing error if the catalog was never installed.
QUERY_SSISDB_EXISTS = "SELECT 1 FROM sys.databases WHERE name = 'SSISDB'"

# Reference queries a LiveSsisCatalogSource issues against SSISDB.
#
# Fully qualified with the "SSISDB." database prefix rather than relying
# on `catalog.*` resolving against the connection's default database --
# the connection is opened against config.source.database (the SOURCE
# data database being discovered, e.g. AdventureWorks2022), not SSISDB,
# so an unqualified reference to `catalog.projects` fails with "Invalid
# object name" (confirmed against a real instance: this was a bug in the
# original unqualified version, found via a live smoke test).
QUERY_PROJECTS = """
SELECT f.name AS folder_name, p.name AS project_name
FROM SSISDB.catalog.projects p
JOIN SSISDB.catalog.folders f ON f.folder_id = p.folder_id
"""

QUERY_PACKAGES = """
SELECT pkg.name AS package_name
FROM SSISDB.catalog.packages pkg
JOIN SSISDB.catalog.projects p ON p.project_id = pkg.project_id
JOIN SSISDB.catalog.folders f ON f.folder_id = p.folder_id
WHERE f.name = ? AND p.name = ?
"""

# catalog.get_project ("Gets a deployed SSIS project as a stream from the
# SSIS Catalog", per Microsoft's documented SSISDB stored procedure
# reference) returns the deployed .ispac as VARBINARY(MAX). An .ispac is a
# standard ZIP archive containing one <PackageName>.dtsx entry per package
# plus Project.params / connection-manager XML, so it's unzipped in-memory
# in Python rather than parsed on the SQL side.
#
# UNVERIFIED AGAINST A LIVE INSTANCE: no SSISDB was reachable while writing
# this (see spike/step0_report.md). catalog.get_project's signature and the
# .ispac-is-a-zip structure are both documented Microsoft behavior, but the
# exact in-archive entry naming (is it always "<PackageName>.dtsx" at the
# archive root, or nested under a folder for some SSIS versions?) should be
# confirmed against a real deployed project before trusting this in
# production. If it doesn't match, adjust `_ISPAC_ENTRY_NAME` below.
QUERY_GET_PROJECT_STREAM = """
DECLARE @project_stream VARBINARY(MAX);
EXEC SSISDB.catalog.get_project @folder_name = ?, @project_name = ?, @project_stream = @project_stream OUTPUT;
SELECT @project_stream;
"""


def _ispac_entry_name(package_name: str) -> str:
    return f"{package_name}.dtsx"


class SsisCatalogSource(Protocol):
    def list_projects(self) -> list[tuple[str, str]]: ...  # (folder, project)
    def list_packages(self, folder: str, project: str) -> list[str]: ...
    def get_package_xml(self, folder: str, project: str, package_name: str) -> str: ...


@dataclass
class LiveSsisCatalogSource:
    connection: "object"  # pyodbc.Connection

    def list_projects(self) -> list[tuple[str, str]]:
        cur = self.connection.cursor()
        cur.execute(QUERY_SSISDB_EXISTS)
        if cur.fetchone() is None:
            logger.info("SSISDB not installed. SSIS discovery skipped.")
            return []

        cur = self.connection.cursor()
        cur.execute(QUERY_PROJECTS)
        return [(r[0], r[1]) for r in cur.fetchall()]

    def list_packages(self, folder: str, project: str) -> list[str]:
        cur = self.connection.cursor()
        cur.execute(QUERY_PACKAGES, folder, project)
        return [r[0] for r in cur.fetchall()]

    def get_package_xml(self, folder: str, project: str, package_name: str) -> str:
        import io
        import zipfile

        cur = self.connection.cursor()
        cur.execute(QUERY_GET_PROJECT_STREAM, folder, project)
        row = cur.fetchone()
        if row is None or row[0] is None:
            raise RuntimeError(f"catalog.get_project returned no stream for {folder}/{project}")
        ispac_bytes = bytes(row[0])

        entry_name = _ispac_entry_name(package_name)
        with zipfile.ZipFile(io.BytesIO(ispac_bytes)) as ispac:
            if entry_name not in ispac.namelist():
                raise RuntimeError(
                    f"{entry_name!r} not found in .ispac for {folder}/{project} "
                    f"-- archive contains: {ispac.namelist()}"
                )
            return ispac.read(entry_name).decode("utf-8")


@dataclass
class FileSystemDtsxSource:
    """Fallback used when packages are file-deployed rather than in
    SSISDB (legacy package deployment model), and also what backs
    `fixture` run mode here since no live SSISDB is reachable."""

    directory: str
    project_name: str = "DiscoveryPilot"

    def list_projects(self) -> list[tuple[str, str]]:
        return [(self.project_name, self.project_name)]

    def list_packages(self, folder: str, project: str) -> list[str]:
        return sorted(f[:-5] for f in os.listdir(self.directory) if f.endswith(".dtsx"))

    def get_package_xml(self, folder: str, project: str, package_name: str) -> str:
        path = os.path.join(self.directory, f"{package_name}.dtsx")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()


def extract_ssis_packages(source: SsisCatalogSource, deployment_model: str = "ssisdb"):
    """Discovers every project/package the source knows about and parses
    each package's XML. One bad package can't fail the whole run --
    failures are isolated per package via @log_object_result."""

    log_entries = []
    packages: list[PackageEntity] = []

    @log_object_result("ssis_project")
    def _list_projects(name):
        return source.list_projects(), "direct_metadata"

    projects, e = _list_projects("catalog")
    log_entries.append(e)

    for folder, project in projects or []:

        @log_object_result("ssis_package_list")
        def _list_packages(name, _folder=folder, _project=project):
            return source.list_packages(_folder, _project), "direct_metadata"

        package_names, e = _list_packages(project)
        log_entries.append(e)

        for pkg_name in package_names or []:

            @log_object_result("ssis_package")
            def _parse_one(name, _folder=folder, _project=project, _pkg_name=pkg_name):
                xml_text = source.get_package_xml(_folder, _project, _pkg_name)
                pkg = parse_dtsx(xml_text, package_name_hint=_pkg_name, project=_project, deployment_model=deployment_model)
                pkg.folder = _folder
                return pkg, "xml_parsed"

            pkg, e = _parse_one(pkg_name)
            log_entries.append(e)
            if pkg is not None:
                packages.append(pkg)

    return packages, log_entries
