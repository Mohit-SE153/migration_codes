"""
Discovery Comparison orchestrator: reads SQLGlot Discovery's and Lakebridge
Discovery's already-written outputs (independently, read-only) and produces
a comparison report. Safe to run even if one or both engines failed or
haven't run yet -- missing output is reported as `not_run`/`failed` in the
comparison rather than raising.

Run: `python -m discovery_comparison.orchestrator`
"""
from __future__ import annotations

from discovery_comparison.comparator import build_comparison
from discovery_comparison.config import load_config
from discovery_comparison.logging_setup import configure_logging, logger
from discovery_comparison.report_writer import write_all_reports


def run_comparison():
    config = load_config()
    configure_logging(config.output_dir)

    logger.info("=== Discovery Comparison started (sqlglot=%s, lakebridge=%s) ===",
                config.sqlglot_output_dir, config.lakebridge_output_dir)

    result = build_comparison(config)
    paths = write_all_reports(result, config.output_dir)

    logger.info("=== Discovery Comparison finished: sqlglot=%s lakebridge=%s reports=%s ===",
                result.sqlglot_run.status, result.lakebridge_run.status,
                ", ".join(str(p) for p in paths.values()))
    return result


if __name__ == "__main__":
    run_comparison()
