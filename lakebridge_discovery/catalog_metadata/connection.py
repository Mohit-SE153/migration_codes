"""
Independent SQL Server connection builder for the catalog_metadata package.

Deliberately re-implements its own connection-string logic rather than
importing lakebridge_discovery.source_exporter._connect_live_sql (and
certainly never anything from autovista/) -- same reasoning source_exporter.py
already documents for staying independent of autovista's own connection
builder: same connection *target*, via the same LakebridgeConfig.source
fields, but never a shared connection object or connection-building
function. This keeps catalog_metadata reviewable/testable on its own and
keeps this engine free of any import path that could pull in autovista.*.

Only this package's probes use this connection -- report_parser.py,
dependency_extractor.py, and source_exporter.py are untouched and keep using
their own existing connections/text exports exactly as before.
"""
from __future__ import annotations

from lakebridge_discovery.config import LakebridgeConfig


def _build_connection_string(config: LakebridgeConfig) -> str:
    """Pure string-building, no I/O -- unit-testable without pyodbc
    installed or a real server reachable."""
    src = config.source
    parts = [
        f"DRIVER={{{src.driver}}}",
        f"SERVER={src.host}",
        f"DATABASE={src.database}",
        f"Encrypt={'yes' if src.encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if src.trust_server_certificate else 'no'}",
    ]
    if src.use_integrated_auth:
        parts.append("Trusted_Connection=yes")
    else:
        parts.append(f"UID={src.username}")
        parts.append(f"PWD={src.password}")
    return ";".join(parts) + ";"


def connect(config: LakebridgeConfig):
    """Opens one new pyodbc connection for a catalog_metadata discovery run.
    Raises RuntimeError with an actionable message if the source isn't
    configured -- same style as source_exporter.py's _connect_live_sql. The
    caller (catalog_metadata.run()) is responsible for catching this and
    recording it as a warning rather than aborting the whole discovery run."""
    import pyodbc  # optional dependency -- only required for live-mode catalog discovery

    src = config.source
    if not src.is_configured:
        raise RuntimeError(
            "catalog_metadata discovery requires AUTOVISTA_SQL_HOST and "
            "AUTOVISTA_SQL_DATABASE to be set -- see .env.example."
        )
    if not src.use_integrated_auth and not (src.username and src.password):
        raise RuntimeError(
            "AUTOVISTA_SQL_USERNAME and AUTOVISTA_SQL_PASSWORD must be set "
            "unless AUTOVISTA_SQL_INTEGRATED_AUTH=true."
        )
    return pyodbc.connect(_build_connection_string(config))
