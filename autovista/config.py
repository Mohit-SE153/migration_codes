"""
Configuration and secrets handling.

Never hardcode credentials. All connection and API secrets are pulled from
environment variables (optionally loaded from a local .env for dev, or from
whatever secrets manager injects them as env vars in a real deployment --
e.g. AWS Secrets Manager / Azure Key Vault / HashiCorp Vault sidecar). See
.env.example for the full variable list.
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
    host: str
    database: str
    username: str | None
    password: str | None
    use_integrated_auth: bool
    driver: str = "ODBC Driver 18 for SQL Server"
    encrypt: bool = True
    # ODBC Driver 18 defaults to strict certificate validation (unlike
    # Driver 17), so it rejects self-signed/internal-CA certs unless this
    # is set. Only bypass validation for trusted internal networks -- it
    # disables MITM protection on the connection.
    trust_server_certificate: bool = False

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.database)


@dataclass(frozen=True)
class LlmFallbackConfig:
    enabled: bool
    api_key: str | None
    model: str
    max_objects_per_run: int


@dataclass(frozen=True)
class AutovistaConfig:
    source: SqlServerConfig
    llm: LlmFallbackConfig
    run_mode: str  # "live" or "fixture"
    state_db_path: str
    output_dir: str
    dtsx_fallback_dir: str | None


def load_config() -> AutovistaConfig:
    """
    Reads all connection/auth details from environment variables. Never
    put real credentials in source control -- see .env.example for the
    documented variable names this expects.
    """
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
    llm = LlmFallbackConfig(
        enabled=os.environ.get("AUTOVISTA_LLM_FALLBACK_ENABLED", "false").lower() == "true",
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        model=os.environ.get("AUTOVISTA_LLM_MODEL", "claude-sonnet-5"),
        max_objects_per_run=int(os.environ.get("AUTOVISTA_LLM_MAX_OBJECTS_PER_RUN", "200")),
    )
    return AutovistaConfig(
        source=source,
        llm=llm,
        run_mode=os.environ.get("AUTOVISTA_RUN_MODE", "fixture"),
        state_db_path=os.environ.get("AUTOVISTA_STATE_DB", "./autovista_state.sqlite3"),
        output_dir=os.environ.get("AUTOVISTA_OUTPUT_DIR", "./output"),
        dtsx_fallback_dir=os.environ.get("AUTOVISTA_DTSX_DIR"),
    )
