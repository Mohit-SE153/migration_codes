"""
Pluggable lineage-extraction engines, so the same raw SQL input can be run
through more than one tool and the results compared (see
lineage_comparison.py for the orchestration/report side of this).

This is a distinct, opt-in evaluation exercise -- NOT part of the core
Discovery pipeline's lineage enrichment. sql_lineage_parser.py (used by
extract_database_metadata's stored-proc/view/embedded-SQL enrichment) is
NOT modified by anything in this module; SqlglotLineageEngine below just
wraps its existing, unmodified `parse_lineage()` function so Discovery's
real output is completely unaffected by this feature existing.

Why Lakebridge's "lineage" here is *derived*, not native:
Databricks Labs Lakebridge (https://databrickslabs.github.io/lakebridge/)
has no documented "give me the referenced tables" API. Its actual,
documented capability is dialect *transpilation* -- converting T-SQL
(source-dialect "mssql", confirmed supported) into Databricks SQL, via a
CLI that operates on a whole folder per invocation
(`databricks labs lakebridge transpile --input-source <dir>
--output-folder <dir> --source-dialect mssql ...`), not a per-string
function call. So "Lakebridge's lineage" in this comparison means:
transpile the input with Lakebridge, then run this project's own
sqlglot-based parse_lineage() (dialect="databricks") against Lakebridge's
*converted* output. That tests whether Lakebridge's conversion preserves
the same table/proc references sqlglot finds directly in the original
T-SQL -- a meaningful signal, but a derived one, not something Lakebridge
reports natively. Lakebridge's own real conversion errors/warnings/exit
code are captured separately as engine_metadata, not folded into the
derived lineage.

UNVERIFIED against a real Lakebridge install (none was available while
building this -- consistent with this codebase's existing pattern for
integrations it couldn't test live, e.g. the SSISDB .ispac entry-naming
and the original live-SQL-Server connection code). Confirm the exact CLI
flags below against `databricks labs lakebridge transpile --help` on a
real install before trusting this in production; override the command
name via AUTOVISTA_LAKEBRIDGE_COMMAND / dialect via
AUTOVISTA_LAKEBRIDGE_SOURCE_DIALECT if your install differs (see config.py).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from autovista.sql_lineage_parser import parse_lineage


@dataclass
class EngineLineageResult:
    object_name: str
    referenced_tables: list[str] = field(default_factory=list)
    referenced_procs: list[str] = field(default_factory=list)
    status: str = "unresolved"  # "resolved" | "unresolved" | "unavailable" | "error"
    notes: str | None = None
    generated_sql_path: str | None = None  # set only by engines that produce converted SQL


@dataclass
class EngineBatchResult:
    engine_name: str
    available: bool
    unavailable_reason: str | None
    duration_ms: float
    results: dict[str, EngineLineageResult] = field(default_factory=dict)
    # Engine-specific extras that don't fit the common per-object shape,
    # e.g. Lakebridge's raw exit code / stderr / error-log path.
    engine_metadata: dict = field(default_factory=dict)


class LineageEngine(Protocol):
    name: str

    def is_available(self) -> tuple[bool, str | None]:
        """Returns (True, None) if this engine can actually run right now,
        else (False, human-readable reason) -- never raises."""
        ...

    def run_batch(self, sql_files: dict[str, str], output_dir: str) -> EngineBatchResult:
        """sql_files: object_name -> raw SQL text. Must never raise --
        failures are reported as EngineBatchResult.available=False or
        per-object status="error", so one engine's failure can never take
        down the other engine's run (see lineage_comparison.py)."""
        ...


@dataclass
class SqlglotLineageEngine:
    """Wraps sql_lineage_parser.parse_lineage() -- the exact function the
    core Discovery pipeline already uses -- unmodified. Always available:
    it's an in-process, pure-Python parse with no external dependency
    beyond the sqlglot package this project already requires."""

    name: str = "sqlglot"

    def is_available(self) -> tuple[bool, str | None]:
        return True, None

    def run_batch(self, sql_files: dict[str, str], output_dir: str) -> EngineBatchResult:
        start = time.perf_counter()
        results: dict[str, EngineLineageResult] = {}
        for object_name, sql_text in sql_files.items():
            try:
                r = parse_lineage(sql_text)
                status = "unresolved" if r.parse_status == "unresolved" else "resolved"
                results[object_name] = EngineLineageResult(
                    object_name=object_name,
                    referenced_tables=r.referenced_tables,
                    referenced_procs=r.referenced_procs,
                    status=status,
                    notes=r.unresolved_reason,
                )
            except Exception as exc:  # noqa: BLE001 - isolate one bad file from the rest of the batch
                results[object_name] = EngineLineageResult(
                    object_name=object_name, status="error", notes=f"{type(exc).__name__}: {exc}",
                )
        duration_ms = (time.perf_counter() - start) * 1000
        return EngineBatchResult(
            engine_name=self.name, available=True, unavailable_reason=None,
            duration_ms=duration_ms, results=results, engine_metadata={},
        )


@dataclass
class LakebridgeLineageEngine:
    """Real integration point for Databricks Labs Lakebridge -- see module
    docstring for why its "lineage" output is derived from its transpile
    output rather than native. Gracefully reports unavailable (never
    raises) when the Databricks CLI / Lakebridge install isn't present,
    matching this codebase's existing pattern for optional external
    dependencies (see llm_fallback_extractor.py's disabled-path handling)."""

    name: str = "lakebridge"
    command: str = "databricks"
    source_dialect: str = "mssql"
    timeout_seconds: int = 600

    def is_available(self) -> tuple[bool, str | None]:
        if shutil.which(self.command) is None:
            return False, (
                f"'{self.command}' CLI not found on PATH -- install the Databricks CLI, "
                f"then run `{self.command} labs install lakebridge`"
            )
        try:
            probe = subprocess.run(
                [self.command, "labs", "lakebridge", "--version"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:  # noqa: BLE001 - any failure here means "not available", not a crash
            return False, f"could not invoke '{self.command} labs lakebridge --version': {exc}"
        if probe.returncode != 0:
            return False, (
                f"'{self.command} labs lakebridge' is not installed/authenticated "
                f"(exit {probe.returncode}): {(probe.stderr or probe.stdout).strip()[:300]}"
            )
        return True, None

    def _unavailable_batch(self, sql_files: dict[str, str], reason: str, duration_ms: float) -> EngineBatchResult:
        return EngineBatchResult(
            engine_name=self.name, available=False, unavailable_reason=reason,
            duration_ms=duration_ms,
            results={
                name: EngineLineageResult(object_name=name, status="unavailable", notes=reason)
                for name in sql_files
            },
            engine_metadata={},
        )

    def run_batch(self, sql_files: dict[str, str], output_dir: str) -> EngineBatchResult:
        start = time.perf_counter()
        available, reason = self.is_available()
        if not available:
            return self._unavailable_batch(sql_files, reason, (time.perf_counter() - start) * 1000)

        converted_dir = Path(output_dir) / "_lakebridge_converted"
        converted_dir.mkdir(parents=True, exist_ok=True)
        error_log_path = Path(output_dir) / "_lakebridge_errors.log"

        with tempfile.TemporaryDirectory(prefix="autovista_lakebridge_input_") as input_dir:
            for object_name, sql_text in sql_files.items():
                (Path(input_dir) / f"{object_name}.sql").write_text(sql_text, encoding="utf-8")

            cmd = [
                self.command, "labs", "lakebridge", "transpile",
                "--input-source", input_dir,
                "--output-folder", str(converted_dir),
                "--source-dialect", self.source_dialect,
                "--skip-validation", "true",
                "--error-file-path", str(error_log_path),
            ]
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=self.timeout_seconds,
                )
                exit_code = proc.returncode
                stderr_tail = (proc.stderr or "")[-2000:] or None
            except Exception as exc:  # noqa: BLE001 - report as engine metadata, not a crash
                exit_code = None
                stderr_tail = f"{type(exc).__name__}: {exc}"

        engine_metadata = {
            "command": cmd,
            "exit_code": exit_code,
            "stderr_tail": stderr_tail,
            "error_log_path": str(error_log_path) if error_log_path.exists() else None,
        }

        results: dict[str, EngineLineageResult] = {}
        for object_name in sql_files:
            converted_path = converted_dir / f"{object_name}.sql"
            if exit_code != 0 or not converted_path.exists():
                results[object_name] = EngineLineageResult(
                    object_name=object_name, status="error",
                    notes=stderr_tail or "Lakebridge did not produce converted output for this object",
                )
                continue
            converted_text = converted_path.read_text(encoding="utf-8")
            try:
                r = parse_lineage(converted_text, dialect="databricks")
                results[object_name] = EngineLineageResult(
                    object_name=object_name,
                    referenced_tables=r.referenced_tables,
                    referenced_procs=r.referenced_procs,
                    status="unresolved" if r.parse_status == "unresolved" else "resolved",
                    notes=r.unresolved_reason,
                    generated_sql_path=str(converted_path),
                )
            except Exception as exc:  # noqa: BLE001 - isolate one bad file from the rest of the batch
                results[object_name] = EngineLineageResult(
                    object_name=object_name, status="error", notes=f"{type(exc).__name__}: {exc}",
                    generated_sql_path=str(converted_path),
                )

        duration_ms = (time.perf_counter() - start) * 1000
        return EngineBatchResult(
            engine_name=self.name, available=True, unavailable_reason=None,
            duration_ms=duration_ms, results=results, engine_metadata=engine_metadata,
        )
