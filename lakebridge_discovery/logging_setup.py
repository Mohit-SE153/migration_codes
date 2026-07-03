"""
Logging for the Lakebridge Discovery engine. Independent logger/handlers
from autovista's -- writes its own discovery_run.log into the Lakebridge
output directory so the two engines' logs never interleave in the same file.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("lakebridge.discovery")


def configure_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [LAKEBRIDGE] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(Path(log_dir) / "discovery_run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
