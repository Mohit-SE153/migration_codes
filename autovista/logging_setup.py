"""
Per-object logging so individual parse failures are triageable without
digging through aggregate run stats. Every extractor call is expected to
go through @log_object_result, which writes one structured line per
object (success or failure) to both console and a run log file, and
returns the accumulated DiscoveryLogEntry list for the run summary.
"""
from __future__ import annotations

import functools
import logging
import time
from pathlib import Path

from autovista.schema import DiscoveryLogEntry

logger = logging.getLogger("autovista.discovery")


def configure_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(Path(log_dir) / "discovery_run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def log_object_result(object_type: str):
    """
    Decorator for per-object extraction functions. The wrapped function
    must accept an object name/identifier as its first positional arg
    (after any leading `self`) and return a (result, parse_status) tuple.
    Exceptions are caught, logged with the object individually identified,
    and re-raised as a sentinel so the caller can skip-and-continue instead
    of failing the whole run.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            object_name = kwargs.get("object_name") or (args[1] if len(args) > 1 else args[0])
            start = time.perf_counter()
            try:
                result, parse_status = fn(*args, **kwargs)
                duration_ms = (time.perf_counter() - start) * 1000
                entry = DiscoveryLogEntry(
                    object_type=object_type,
                    object_name=str(object_name),
                    status="success",
                    parse_status=parse_status,
                    duration_ms=round(duration_ms, 2),
                )
                logger.info(
                    "OK   %-12s %-45s parse_status=%s (%.1fms)",
                    object_type, object_name, parse_status, duration_ms,
                )
                return result, entry
            except Exception as exc:  # noqa: BLE001 - intentional: isolate one object's failure
                duration_ms = (time.perf_counter() - start) * 1000
                entry = DiscoveryLogEntry(
                    object_type=object_type,
                    object_name=str(object_name),
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=round(duration_ms, 2),
                )
                logger.error(
                    "FAIL %-12s %-45s error=%s (%.1fms)",
                    object_type, object_name, entry.error, duration_ms,
                )
                return None, entry

        return wrapper

    return decorator
