"""
Configuration for the Discovery Comparison module. Independent of both
engines' config modules -- this one only needs to know where each engine's
output already landed (it is a read-only consumer of both, never a
dependency of either engine)."""
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
class ComparisonConfig:
    sqlglot_output_dir: str
    lakebridge_output_dir: str
    output_dir: str


def load_config() -> ComparisonConfig:
    return ComparisonConfig(
        sqlglot_output_dir=os.environ.get("AUTOVISTA_OUTPUT_DIR", "./output"),
        lakebridge_output_dir=os.environ.get("LAKEBRIDGE_OUTPUT_DIR", "./output_lakebridge"),
        output_dir=os.environ.get("COMPARISON_OUTPUT_DIR", "./output_comparison"),
    )
