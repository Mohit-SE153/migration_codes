"""
Configuration for the Lakebridge Discovery engine.

Deliberately independent of autovista/config.py -- this module has its own
env-var loading so the two Discovery engines never share code, only (where
it makes sense) the same env var *names* for things that must genuinely be
identical between them, like which source database to point at and which
run mode to use. Everything Lakebridge-specific uses a LAKEBRIDGE_ prefix.

Never hardcode credentials -- see .env.example.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _load_dotenv_if_present() -> None:
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv_if_present()


@dataclass(frozen=True)
class SqlServerConfig:
    """Same source database as autovista/config.py's SqlServerConfig --
    reads the same AUTOVISTA_SQL_* env vars, since both engines must point
    at the identical source database. This is the one deliberate exception
    to "no shared code": shared *connection target*, via shared env var
    names, not a shared connection object or shared query code."""

    host: str
    database: str
    username: str | None
    password: str | None
    use_integrated_auth: bool
    driver: str = "ODBC Driver 18 for SQL Server"
    encrypt: bool = True
    trust_server_certificate: bool = False

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.database)


@dataclass(frozen=True)
class LakebridgeConfig:
    enabled: bool
    run_mode: str  # "live" or "fixture" -- same convention as AUTOVISTA_RUN_MODE
    source: SqlServerConfig
    dtsx_fallback_dir: str | None

    cli_path: str  # path/name of the `databricks` CLI executable
    source_tech_sql: str  # --source-tech value for exported SQL DDL/definitions
    source_tech_etl: str  # --source-tech value for exported SSIS packages
    generate_json: bool
    analyze_timeout_seconds: int

    output_dir: str
    source_export_dir: str  # working directory where exported source files are staged for the analyzer


def load_config() -> LakebridgeConfig:
    source = SqlServerConfig(
        host=os.environ.get("AUTOVISTA_SQL_HOST", ""),
        database=os.environ.get("AUTOVISTA_SQL_DATABASE", ""),
        username=os.environ.get("AUTOVISTA_SQL_USERNAME"),
        password=os.environ.get("AUTOVISTA_SQL_PASSWORD"),
        use_integrated_auth=os.environ.get("AUTOVISTA_SQL_INTEGRATED_AUTH", "false").lower() == "true",
        driver=os.environ.get("AUTOVISTA_SQL_DRIVER", "ODBC Driver 18 for SQL Server"),
        encrypt=os.environ.get("AUTOVISTA_SQL_ENCRYPT", "true").lower() == "true",
        trust_server_certificate=os.environ.get("AUTOVISTA_SQL_TRUST_SERVER_CERT", "false").lower() == "true",
    )
    return LakebridgeConfig(
        enabled=os.environ.get("LAKEBRIDGE_ENABLED", "true").lower() == "true",
        run_mode=os.environ.get("AUTOVISTA_RUN_MODE", "fixture"),
        source=source,
        dtsx_fallback_dir=os.environ.get("AUTOVISTA_DTSX_DIR"),
        cli_path=os.environ.get("LAKEBRIDGE_CLI_PATH", "databricks"),
        source_tech_sql=os.environ.get("LAKEBRIDGE_SOURCE_TECH_SQL", "MS SQL Server"),
        source_tech_etl=os.environ.get("LAKEBRIDGE_SOURCE_TECH_ETL", "SSIS"),
        generate_json=os.environ.get("LAKEBRIDGE_GENERATE_JSON", "true").lower() == "true",
        analyze_timeout_seconds=int(os.environ.get("LAKEBRIDGE_ANALYZE_TIMEOUT_SECONDS", "1800")),
        output_dir=os.environ.get("LAKEBRIDGE_OUTPUT_DIR", "./output_lakebridge"),
        source_export_dir=os.environ.get("LAKEBRIDGE_SOURCE_EXPORT_DIR", "./output_lakebridge/_source_export"),
    )
