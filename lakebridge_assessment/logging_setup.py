"""Run logging for the Lakebridge Assessment phase -- same console + file
handler pattern as assessment/logging_setup.py, kept as a separate module
so this package stays importable without pulling in the sqlglot-side
assessment package at all."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("lakebridge_assessment")


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

    file_handler = logging.FileHandler(Path(log_dir) / "lakebridge_assessment_run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
