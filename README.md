# Autovista — Discovery Phase

Discovery-phase pipeline for the Autovista legacy-to-modern (SQL Server/SSIS →
target platform) migration accelerator. This build covers **Discovery only**:
it produces a structured inventory of a source SQL Server/SSIS estate plus a
cross-object dependency graph. It does not score complexity, generate
migration code, or validate anything post-migration — those are separate
downstream phases that consume this phase's output.

Read `spike/step0_report.md` first — it's the evidence-based comparison of
sqlglot vs. Databricks Lakebridge vs. LLM extraction that this pipeline's
architecture is based on, including what was actually measured vs. what is
still unverified.

## Scope of this build

- **Target environment**: on-prem SQL Server with SSIS deployed via the
  SSISDB catalog (the SSISDB catalog is the primary discovery path; falling
  back to file-deployed `.dtsx` uses the same XML parser on file-system
  bytes instead of catalog-retrieved bytes).
- **Assumed scale**: a small pilot estate — tens of databases, hundreds of
  tables, dozens of SSIS packages. The pipeline's design (per-object
  logging, SQLite state store, no external orchestration dependency) will
  work at the prompt's stated "up to 500 databases / 5,000 tables / 2,000
  packages" target too, but has only been exercised at pilot scale here —
  re-validate throughput before pointing it at a much larger estate.
- **No live SQL Server/SSISDB was reachable while building this** — see
  "Run modes" below for how the pipeline is still fully runnable and tested.

## Run modes

| Mode | What it does | When to use |
|---|---|---|
| `fixture` (default) | Runs entirely against the synthetic sample environment in `fixtures/` — no network, no credentials. | Local dev, CI, and everything demonstrated in this build. |
| `live` | Connects to a real SQL Server + SSISDB via `pyodbc` using `LiveSqlServerSource` / `LiveSsisCatalogSource`. | Real deployment. **Status: validated against a real SQL Server 2022 instance** (`AdventureWorks2022`) — direct-metadata extraction (databases, tables, procs, views, triggers, foreign keys) and sqlglot lineage parsing were both run end-to-end and several real bugs were found and fixed in the process (see `spike/step0_report.md` → "Live validation addendum" for the full list — TL;DR: an `OPTION(...)` query-hint clause, a CTE self-reference leak, and `BEGIN TRY/CATCH` silently dropping table references were all fixed and now have regression tests). **Still not verified**: `catalog.get_project`'s `.ispac`-zip entry naming in `LiveSsisCatalogSource.get_package_xml`, since the test instance had no `SSISDB` catalog database to validate against (SSIS extraction correctly fails with a clean, isolated error in that case rather than crashing the run — also confirmed live). Run against an instance with real SSIS packages deployed before trusting that specific path. |

Set the mode via `AUTOVISTA_RUN_MODE` (see `.env.example`).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fixture mode needs no further edits
```

`pyodbc` is in `requirements.txt` and only actually used in `live` mode —
fixture mode never imports it. For `live` mode you also need a system ODBC
driver installed (e.g. "ODBC Driver 18 for SQL Server"), and fill in `.env`:

```
AUTOVISTA_RUN_MODE=live
AUTOVISTA_SQL_HOST=...
AUTOVISTA_SQL_DATABASE=...
AUTOVISTA_SQL_USERNAME=...        # read-only service account — never a privileged login
AUTOVISTA_SQL_PASSWORD=...        # or set AUTOVISTA_SQL_INTEGRATED_AUTH=true instead
```

**Common first-connection error:** `08001 ... SSL routines::certificate
verify failed: self-signed certificate`. ODBC Driver 18 validates the
server certificate strictly by default and will reject a self-signed or
internal-CA cert. If that's your environment, set
`AUTOVISTA_SQL_TRUST_SERVER_CERT=true` in `.env` — only do this on a
trusted internal network, since it disables MITM protection on the
connection.

**Credentials are never hardcoded.** `autovista/config.py` reads everything
from environment variables (optionally via `.env` for local dev). In a real
deployment, populate those same environment variables from your secrets
manager (AWS Secrets Manager, Azure Key Vault, Vault, etc.) — nothing in
this codebase assumes a specific one.

The service account used in `live` mode should be **read-only,
least-privilege**: `VIEW SERVER STATE`, `VIEW ANY DEFINITION`, and
`db_datareader` on SSISDB + source databases. Discovery never writes to the
source environment — there is no code path in this pipeline that executes
anything other than `SELECT`/metadata queries.

## Running discovery

```bash
python3 -m autovista.orchestrator
```

This runs `fixture` mode by default, writing to `./output/`:

- `discovery_manifest.json` — the full nested manifest (schema below)
- `discovery_rollup.csv` — flat counts/sizes for a 30-second human sanity check
- `discovery_log_summary.csv` — one row per extracted object, success or failure
- `discovery_run.log` — the same information as a plain-text run log

State (for idempotent re-runs) is kept in a local SQLite file,
`autovista_state.sqlite3` by default (`AUTOVISTA_STATE_DB`).

### Idempotency / resumability

Every table, stored procedure, and SSIS package is fingerprinted (a hash of
its row-count/size for tables, its T-SQL definition for procs, its raw XML
bytes for packages). Re-running discovery skips anything whose fingerprint
hasn't changed since the last run — logged as `SKIP ... unchanged since
last run` — instead of re-parsing it. Try it:

```bash
python3 -m autovista.orchestrator   # first run: scans everything
python3 -m autovista.orchestrator   # second run: skips unchanged objects
```

### Error isolation

One broken `.dtsx` file or one stored procedure that fails to parse does not
fail the run. Each extraction is wrapped individually (see
`autovista/logging_setup.py::log_object_result`); failures are logged
per-object with the specific error and the run continues. See
`tests/test_error_isolation.py` for a demonstration (a deliberately
malformed `.dtsx` file alongside good ones, and a metadata query rigged to
raise).

## Running the test suite

```bash
python3 -m pytest tests/ -v
```

## Running the Step 0 spike yourself

```bash
python3 spike/spike_runner.py   # regenerates spike/spike_results.json
```

## Project layout

```
autovista/                      # the actual Discovery pipeline (production code)
  config.py                     # env-var-driven config, no hardcoded secrets
  schema.py                     # output contract dataclasses (the JSON manifest shape)
  logging_setup.py              # per-object success/failure logging
  state_store.py                # SQLite-backed idempotent/resumable run state
  sql_metadata_extractor.py     # databases/tables/columns/procs/triggers/agent jobs (direct_metadata)
  sql_lineage_parser.py         # sqlglot-based T-SQL -> table/proc reference extraction
  dtsx_xml_parser.py            # .dtsx XML -> tasks/connmgrs/variables/precedence/embedded SQL
  ssis_catalog_extractor.py     # SSISDB catalog / file-deployed package discovery
  llm_fallback_extractor.py     # last-resort LLM extraction for unparseable constructs
  dependency_graph_builder.py   # combines everything above into dependencies[]
  output_writer.py              # JSON manifest + CSV rollup + log summary
  orchestrator.py               # wires it all together; `python -m autovista.orchestrator`

fixtures/                       # synthetic sample environment + demo-mode data sources
  sql/ddl_sample.sql            # source-of-truth DDL for the fixture "SalesDW" database
  dtsx/*.dtsx                   # 5 sample SSIS packages of varying complexity
  mock_catalog.py               # parses ddl_sample.sql into fixture-mode catalog data
                                 # (NOT part of the production extraction path)

spike/
  step0_report.md               # Step 0 deliverable: method comparison + recommendation
  spike_runner.py                # regenerates the measured numbers in step0_report.md
  spike_results.json

output/sample_run/              # sample discovery output committed for reference
tests/                          # unit + error-isolation tests
```

## Output schema

The manifest (`discovery_manifest.json`) is a single nested JSON object —
chosen over N normalized files because pilot/small-estate scale doesn't need
the split, and one file is simpler for the Assessment phase to consume. If a
future larger-estate run needs streaming/partitioned output, the entity
lists are already normalized internally (`autovista/schema.py`) and can be
split file-per-entity-type without changing any extractor code.

Top-level keys and their entity shape (see `autovista/schema.py` for exact
dataclass definitions):

```
databases[]        { name, size_mb, table_count, proc_count, view_count, parse_status }
tables[]            { database, schema, name, row_count, size_mb, column_count, columns[], parse_status }
views[]             { database, schema, name, referenced_tables[], parse_status }
triggers[]          { database, schema, name, table, event, parse_status }
agent_jobs[]        { name, enabled, steps[], parse_status }
stored_procedures[] { database, schema, name, loc, referenced_tables[], referenced_procs[], parse_status, unresolved_reason }
packages[]          { name, project, deployment_model, tasks[], connection_managers[], variables[],
                       precedence_constraints[], embedded_sql[], parse_status }
dependencies[]      { source_object, source_type, target_object, target_type, relationship_type, discovery_method }
```

**`parse_status`** appears on every entity and indicates exactly how that
entity's data was produced — this is the traceability the Assessment phase
needs to weight confidence:

| Value | Meaning |
|---|---|
| `direct_metadata` | Queried directly from system catalog views/DMVs. Ground truth — used for all row counts, sizes, and object existence. Never inferred. |
| `xml_parsed` | Extracted by parsing `.dtsx` XML structure (control flow, connection managers, variables). Deterministic. |
| `sqlglot` | T-SQL lineage (table/proc references) resolved by parsing SQL text with sqlglot. Deterministic given the parser's dialect coverage — see the Step 0 report for several real edge cases found and fixed via live validation. **Check `unresolved_reason` even when `parse_status` is `sqlglot`**: if it's non-null, part of the statement fell back to an unparsed block and `referenced_tables`/`referenced_procs` may be incomplete (a best-effort regex supplement is applied, but isn't guaranteed complete) — treat as lower-confidence, same as `unresolved`/`llm_inferred`, rather than assuming a non-`unresolved` `parse_status` always means a complete result. |
| `llm_inferred` | Produced by the LLM fallback for a construct no static parser could resolve (dynamic SQL, Script Task source). Always paired with `needs_human_review=True` semantics upstream — never treated as ground truth. |
| `unresolved` | Explicitly flagged as unparseable and NOT guessed — either the LLM fallback was unavailable/disabled, or it declined to produce a confident answer. Surfaced for human review, same as `llm_inferred`. |

**`dependencies[].relationship_type`** values: `reads` (proc/package →
table), `calls` (proc/package → proc), `executes` (package → package via
Execute Package Task), `foreign_key` (table → table).

**`dependencies[].discovery_method`** reuses the same `parse_status` values
above, so a downstream confidence-weighting step can treat both consistently.

## Non-goals (explicit, per the build spec)

This phase does **not** do complexity/effort scoring, PySpark or any other
migration code generation, or reconciliation/validation. Constructs this
pipeline can't confidently parse are flagged (`llm_inferred` / `unresolved`)
for human review, never guessed at.

---

# Multi-engine Discovery: SQLGlot vs. Lakebridge

Everything above this line describes the original SQLGlot Discovery
pipeline (`autovista/`), **unchanged**. This section documents a second,
completely independent Discovery engine — **Lakebridge Discovery**
(`lakebridge_discovery/`) — added purely to compare Discovery capability
against SQLGlot Discovery, plus the **Discovery Comparison** module
(`discovery_comparison/`) that reads both engines' already-written output
and reports the differences.

This is still Discovery-phase only. Neither engine does SQL-to-SQL
conversion, transpilation, or generates migrated code — Lakebridge's
`analyze` (Analyzer/Assessment) subcommand is the only Lakebridge feature
used here; its `transpile`/`reconcile` subcommands are never invoked.

## Architecture

```
Source Database
    ├──► SQLGlot Discovery      (autovista/)              → ./output/
    └──► Lakebridge Discovery   (lakebridge_discovery/)    → ./output_lakebridge/

           (both outputs, read-only, after both finish or fail)
                          │
                          ▼
                Discovery Comparison (discovery_comparison/) → ./output_comparison/
```

- **SQLGlot Discovery and Lakebridge Discovery never import each other**,
  never read each other's output, and never call each other. The only
  thing they share is which source database to point at (same
  `AUTOVISTA_SQL_*` / `AUTOVISTA_RUN_MODE` env vars) and, in `fixture`
  mode, the same `fixtures/` sample DDL/`.dtsx` files as raw input.
- A failure in one engine does not stop the other, and does not stop the
  comparison step — see `run_all_discovery.py`.
- **Output folders are completely separate.** `./output/` (SQLGlot) is
  untouched by this work. `./output_lakebridge/` and `./output_comparison/`
  are new and neither engine writes into the other's folder.

## Why Lakebridge Discovery needs a source-export step

Unlike SQLGlot Discovery (which queries SQL Server live via `pyodbc`),
the Lakebridge Analyzer (`databricks labs lakebridge analyze`) only
accepts a **directory of source files** as input — it has no live-database
connection mode. So in `live` run mode, `lakebridge_discovery/source_exporter.py`
first stages the source database into files:

- Table DDL is best-effort reconstructed from `INFORMATION_SCHEMA.COLUMNS`
  (SQL Server doesn't store table DDL as text the way it does for views/
  procs/functions/triggers, so this is columns-and-types only — not a
  byte-perfect `CREATE TABLE`, no constraints/indexes/defaults).
- Views, stored procedures, functions, and trigger bodies are exported
  verbatim from `sys.sql_modules.definition`.
- SSIS packages are exported from the same place SQLGlot Discovery would
  read them from (file-deployed `.dtsx` directory, or the SSISDB catalog).

This export uses its **own independent SQL Server connection code** (not
`autovista/sql_metadata_extractor.py`) — it's a raw text/XML dump, not a
parsed discovery result, so staging it doesn't make Lakebridge depend on
SQLGlot's output. In `fixture` mode, no export is needed: it points the
Analyzer directly at `fixtures/sql/ddl_sample.sql` and `fixtures/dtsx/*.dtsx`.

The Analyzer is then invoked once per `--source-tech` (`mssql` for the
exported SQL, `ssis` for the exported packages), since the CLI takes one
source-tech per run.

## Installing Lakebridge

Lakebridge is a separate Databricks Labs tool, not a Python library this
project imports — `lakebridge_discovery/lakebridge_runner.py` shells out to
it. Prerequisites (per [Lakebridge's install docs](https://databrickslabs.github.io/lakebridge/docs/installation/)):

1. **A Databricks workspace** (any type, including a free trial) and the
   **Databricks CLI**, authenticated (`databricks configure`, PAT or
   service principal).
2. **Java 21+** (`java -version`) — required by Lakebridge's Morpheus
   transpiler component even though Discovery here never invokes
   transpilation.
3. **Python 3.10–3.14**.
4. Network access to GitHub, Maven Central, and PyPI (the install pulls
   dependencies from all three).

Install:

```bash
databricks labs install lakebridge
```

No further `install-transpile` / `configure-reconcile` step is needed —
those set up Lakebridge's conversion/reconciliation features, which this
integration deliberately never uses.

**This sandbox does not currently have the Databricks CLI, a workspace, or
Java 21 available (Java 11 is installed)** — `lakebridge_discovery` is
built to call the real CLI, and will log a clean, isolated failure for the
`analyze` step here (per the error-isolation design) until those
prerequisites are provisioned. SQLGlot Discovery is completely unaffected
either way.

**Lakebridge's Analyzer report schema (JSON/Excel) is not publicly
documented at the field level.** `lakebridge_discovery/report_parser.py`
maps it defensively (tolerates missing/renamed fields, never crashes on an
unrecognized shape) and every `LakebridgeDiscoveryResult` carries
`mapping_verified=False` with a note to that effect until someone runs
this against a real workspace and the field mapping in `report_parser.py`
is confirmed/corrected against real output — same "validate before
trusting" principle as `spike/step0_report.md`.

## Running SQLGlot Discovery

Unchanged — see "Running discovery" above:

```bash
python3 -m autovista.orchestrator
```

## Running Lakebridge Discovery

```bash
pip install -r requirements.txt   # now also installs openpyxl
python3 -m lakebridge_discovery.orchestrator
```

Writes to `./output_lakebridge/` (`LAKEBRIDGE_OUTPUT_DIR`):

- `lakebridge_manifest.json` — full result (export summary, analyze
  invocations, inventory, dependencies, warnings/errors, `mapping_verified`)
- `tables.json`, `views.json`, `stored_procedures.json`, `functions.json`,
  `triggers.json`, `synonyms.json`, `schemas.json`, `packages.json`,
  `unsupported_objects.json`, `dependencies.json` — per-category files
- `lakebridge_rollup.csv` — flat counts
- `lakebridge_log_summary.csv`, `discovery_run.log`
- `reports/lakebridge_report_<source-tech>.xlsx` / `.json` — the raw
  Lakebridge Analyzer report(s), kept for manual inspection

Set `LAKEBRIDGE_ENABLED=false` to skip this engine entirely (e.g. before
the Databricks CLI/workspace prerequisites are set up).

## Running both engines together + generating the comparison

```bash
python3 run_all_discovery.py
```

Runs SQLGlot Discovery, then Lakebridge Discovery, then the comparison —
each step isolated from the others' failures. Equivalent to running the
three commands below in sequence, which also works if you want to inspect
each engine's output before moving to the next step:

```bash
python3 -m autovista.orchestrator
python3 -m lakebridge_discovery.orchestrator
python3 -m discovery_comparison.orchestrator
```

## Discovery Comparison report

`discovery_comparison/` reads `./output/` and `./output_lakebridge/`
**read-only** (it does not modify either) and writes to
`./output_comparison/` (`COMPARISON_OUTPUT_DIR`):

- `comparison_report.json` — full structured comparison
- `comparison_report.csv` — one row per object category (tables, views,
  stored procedures, functions, triggers, synonyms, SSIS packages,
  dependencies, unsupported objects) with both engines' counts and the
  difference
- `comparison_report.md` — human-readable report: engine run status
  (success/partial/failed/not_run, duration, error/warning counts), the
  same category table, and a best-effort sample of object names found by
  one engine but not the other

Safe to run even if one or both engines haven't run yet or failed —
missing output is reported as `not_run`/`failed` rather than raising.
Name-based "found by one engine but not the other" matching is
best-effort (see `discovery_comparison/comparator.py`'s docstring) —
**counts are the reliable signal**, name-level matching is a bonus, not a
guaranteed-precise join.

## Adding a third Discovery engine later

Each engine is a self-contained package with the same shape (`config.py`,
`logging_setup.py`, `schema.py`, an `orchestrator.py` exposing
`run_discovery()`, and its own output directory). To add another engine:
write a new package following that shape, add it as a third independent
step in `run_all_discovery.py` (wrapped in its own try/except, same as the
other two), and extend `discovery_comparison/comparator.py`'s category
loop to also read its output directory. No existing engine's code changes.
