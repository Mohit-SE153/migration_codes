"""
Runs both Discovery engines independently, then generates the comparison
report. This is the only module that touches all three -- SQLGlot
Discovery and Lakebridge Discovery never import each other, and this script
does not feed one engine's output into the other; it just sequences three
independent steps and isolates failures between them.

A failure in one engine is caught here and logged, but does not stop the
other engine from running, and does not stop the comparison step from
running afterwards (the comparison itself reports the failure instead of
crashing -- see discovery_comparison/comparator.py).

Run: `python run_all_discovery.py`
Equivalent to running these separately:
    python -m autovista.orchestrator
    python -m lakebridge_discovery.orchestrator
    python -m discovery_comparison.orchestrator
"""
from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [RUN-ALL] %(message)s")
logger = logging.getLogger("run_all_discovery")


def _run_sqlglot() -> bool:
    from autovista.orchestrator import run_discovery as run_sqlglot_discovery

    logger.info("Starting SQLGlot Discovery...")
    start = time.perf_counter()
    try:
        run_sqlglot_discovery()
        logger.info("SQLGlot Discovery finished successfully in %.2fs", time.perf_counter() - start)
        return True
    except Exception:  # noqa: BLE001 - a SQLGlot failure must not stop Lakebridge or the comparison
        logger.exception("SQLGlot Discovery failed after %.2fs -- continuing with Lakebridge Discovery", time.perf_counter() - start)
        return False


def _run_lakebridge() -> bool:
    from lakebridge_discovery.orchestrator import run_discovery as run_lakebridge_discovery

    logger.info("Starting Lakebridge Discovery...")
    start = time.perf_counter()
    try:
        result = run_lakebridge_discovery()
        logger.info("Lakebridge Discovery finished with status=%s in %.2fs", result.status, time.perf_counter() - start)
        return result.status in ("success", "partial")
    except Exception:  # noqa: BLE001 - a Lakebridge failure must not stop the comparison step
        logger.exception("Lakebridge Discovery failed after %.2fs -- continuing to comparison", time.perf_counter() - start)
        return False


def _run_comparison() -> bool:
    from discovery_comparison.orchestrator import run_comparison

    logger.info("Starting Discovery Comparison...")
    start = time.perf_counter()
    try:
        run_comparison()
        logger.info("Discovery Comparison finished in %.2fs", time.perf_counter() - start)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Discovery Comparison failed after %.2fs", time.perf_counter() - start)
        return False


def main() -> int:
    logger.info("=== Running SQLGlot Discovery + Lakebridge Discovery + Comparison ===")
    sqlglot_ok = _run_sqlglot()
    lakebridge_ok = _run_lakebridge()
    comparison_ok = _run_comparison()

    logger.info(
        "=== Run complete: sqlglot=%s lakebridge=%s comparison=%s ===",
        "OK" if sqlglot_ok else "FAILED",
        "OK" if lakebridge_ok else "FAILED",
        "OK" if comparison_ok else "FAILED",
    )
    return 0 if comparison_ok else 1


if __name__ == "__main__":
    sys.exit(main())
