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
  compatibility_scanner.py      # sqlglot AST + regex scan for named migration-risk constructs
  compatibility_remediation.py  # LLM-assisted "why is this flag risky" note for flagged objects
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
server_instance     { product_version, product_level, edition, engine_edition, machine_name, instance_name,
                       cpu_count, physical_memory_mb, max_server_memory_mb, parse_status }
                    -- singular, not a list: one per Discovery run (server-scoped, SERVERPROPERTY()/
                       sys.dm_os_sys_info/sys.configurations), regardless of how many databases are scanned.
                       null if server-level discovery itself failed (see server_instance.json).
databases[]        { name, size_mb, table_count, proc_count, view_count, parse_status }
tables[]            { database, schema, name, row_count, size_mb, column_count, columns[], parse_status,
                       is_temporal_table, is_memory_optimized, is_cdc_enabled, is_change_tracking_enabled,
                       is_partitioned, partition_count, compression }
                    -- compression is the dominant/single data_compression_desc value across the table's
                       partitions (sys.partitions, index_id IN (0,1)); if a table's partitions carry
                       different compression settings, compression is reported as "MIXED (a, b, ...)"
                       rather than silently picking one.
indexes[]           { database, schema, table, name, index_type, is_clustered, is_nonclustered, is_unique,
                       is_primary_key, is_disabled, is_filtered, key_columns[], included_columns[], ... }
                    -- index_type is sys.indexes.type_desc verbatim: CLUSTERED/NONCLUSTERED (relational),
                       XML, SPATIAL, CLUSTERED COLUMNSTORE/NONCLUSTERED COLUMNSTORE, and NONCLUSTERED HASH
                       (memory-optimized) are all included -- every named, deployed index type a migration
                       planner needs to see, even ones with no direct Databricks equivalent (an XML index
                       should be *flagged* for redesign, not silently dropped from inventory). Two
                       exclusions only, both structural: the type=0 HEAP placeholder row every heap table
                       has (not a named index object; heap-ness is on TableEntity.table_type instead), and
                       is_hypothetical=1 rows (Database Engine Tuning Advisor "what-if" indexes -- never
                       deployed, not real schema). See QUERY_INDEXES in sql_metadata_extractor.py for the
                       full index-category analysis this scope is drawn from.
views[]             { database, schema, name, referenced_tables[], parse_status, compatibility_flags[] }
triggers[]          { database, schema, name, table, event, parse_status, compatibility_flags[] }
agent_jobs[]        { name, enabled, steps[], parse_status, step_details[] }
                    -- step_details[].referenced_tables[]/referenced_procs[]/parse_status/unresolved_reason
                       are populated only for subsystem="TSQL" steps, by running that step's command text
                       through the same sql_lineage_parser.parse_lineage() used for stored procedure
                       bodies (see orchestrator.py's agent-job enrichment loop). CmdExec/PowerShell/other
                       subsystems are left unparsed (not T-SQL) -- these feed the agent_job -> table/
                       procedure dependency edges (see dependency_graph_builder.py).
stored_procedures[] { database, schema, name, loc, referenced_tables[], referenced_procs[], parameters[],
                       parameter_count, dynamic_sql_usage, parse_status, unresolved_reason, compatibility_flags[] }
                    -- parameters[] (sys.parameters joined to sys.types, ordered by parameter_id) also
                       populates FunctionEntity.parameters/parameter_count; mode is "OUT" for output
                       parameters (sys.parameters.is_output = 1), else "IN".
                    -- dynamic_sql_usage is set from the same dynamic-SQL detection
                       (sp_executesql / EXEC(@var)) that already drives parse_status="unresolved"
                       for that object -- see sql_lineage_parser.py's DYNAMIC_SQL_MARKERS.
functions[]         { database, schema, name, function_type, return_type, parameters[], parameter_count,
                       parse_status, compatibility_flags[] }
packages[]          { name, project, deployment_model, tasks[], connection_managers[], variables[],
                       precedence_constraints[], embedded_sql[], parse_status }
                    -- each embedded_sql[] entry also carries compatibility_flags[].
dependencies[]      { source_object, source_type, target_object, target_type, relationship_type, discovery_method }
security_principals[] { database, name, principal_type, scope, ... }
                    -- scope is "database" (existing sys.database_principals rows, principal_type
                       "USER"/"ROLE") or "server" (sys.server_principals rows, principal_type
                       "LOGIN"/"SERVER_ROLE", database=""). Server-scoped rows also carry
                       member_of_roles[] (sys.server_role_members). One discovery run only ever
                       fetches server-scoped principals once, not once per database.
permissions[]       { database, grantee, principal_type, scope, ... } -- same scope discriminator
                       as security_principals[] ("server" rows: sys.server_permissions, database="").
linked_servers[]    { name, product, provider, data_source, provider_string_redacted, parse_status }
                    -- singular list, server-scoped (sys.servers WHERE is_linked = 1), not per-database.
                       provider_string_redacted has any password=/pwd=... substring replaced with
                       ***REDACTED*** (same defensive pattern as dtsx_xml_parser.py's connection-string
                       redaction), even though this hasn't been observed to contain one in practice.
```

**`compatibility_flags[]`** (on `stored_procedures[]`, `views[]`, `functions[]`, `triggers[]`, and each
`packages[].embedded_sql[]` entry) is produced by `autovista/compatibility_scanner.py` scanning that
object's already-fetched definition/SQL text for named SQL-Server-feature migration-risk constructs --
`PIVOT`, `UNPIVOT`, `CROSS_APPLY`, `OUTER_APPLY`, `MERGE`, `OPENJSON`, `FOR_XML`, `FOR_JSON`, `OPENQUERY`,
`OPENDATASOURCE`, `LINKED_SERVER`, `XP_CMDSHELL`, `SP_OA`. Empty list means none of these specific
constructs were detected in that object -- not a general "this SQL is migration-clean" verdict.
`discovery_rollup.csv` includes one `compatibility_flag` row per distinct flag with its count across
the whole database, so `grep MERGE discovery_rollup.csv` gives a same-day answer to "how many objects
use MERGE" without opening the manifest.

**`compatibility_notes`** (same objects as `compatibility_flags[]` above) is an optional, off-by-default
LLM-assisted enrichment produced by `autovista/compatibility_remediation.py` (see
`AUTOVISTA_LLM_COMPAT_NOTES_ENABLED` in `.env.example`): a short plain-English note on why a flagged
object's construct is a migration risk on Databricks/Spark SQL and roughly what rework it implies. It
never adds, removes, or reinterprets a flag — `compatibility_flags[]` is always produced solely by the
deterministic scanner — and is `null` unless the feature is enabled, an API key is configured, and the
object actually has flags. Every non-null note is implicitly `needs_human_review` — a starting point for
a reviewer, never an accepted migration plan. `discovery_rollup.csv` includes one
`compatibility_notes_generated` aggregate row for how many flagged objects got a note this run. This is a
second, independent use of the LLM from `llm_fallback_extractor.py` above — it has its own enable flag
(`AUTOVISTA_LLM_COMPAT_NOTES_ENABLED`) and object cap (`AUTOVISTA_LLM_COMPAT_MAX_OBJECTS_PER_RUN`), so
turning one on/off never changes the other's budget.

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
for human review, never guessed at. Complexity/effort scoring is now covered
by the Assessment phase (`assessment/`) — see the "Assessment Phase" section
at the end of this file.

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
  `unsupported_objects.json`, `dependencies.json` — per-category files,
  from the Analyzer report inventory (`report_parser.py`). Every object in
  `tables`/`views`/`stored_procedures`/`functions`/`triggers` also carries
  a `compatibility_flags` list — named migration-risk constructs
  (`PIVOT`/`UNPIVOT`/`CROSS_APPLY`/`OUTER_APPLY`/`MERGE`/`OPENJSON`/
  `LINKED_SERVER`/`FOR_XML`/`FOR_JSON`/`OPENQUERY`/`OPENDATASOURCE`/
  `XP_CMDSHELL`/`SP_OA`) found by scanning that object's own exported
  definition text — see `lakebridge_discovery/compatibility_scanner.py`,
  an independent reimplementation of `autovista/compatibility_scanner.py`'s
  detection (never an import of it), wired in by `orchestrator.py` right
  after dependency extraction. Objects with a non-empty `compatibility_flags`
  also get a `compatibility_notes` field — an optional, off-by-default
  LLM-assisted remediation note (`LAKEBRIDGE_LLM_COMPAT_NOTES_ENABLED` in
  `.env.example`), produced by `lakebridge_discovery/compatibility_remediation.py`.
  Same independent-reimplementation relationship to
  `autovista/compatibility_remediation.py` as the scanner above — its own
  enable flag, model, and object cap (`LAKEBRIDGE_LLM_MODEL`,
  `LAKEBRIDGE_LLM_COMPAT_MAX_OBJECTS_PER_RUN`), sharing only
  `ANTHROPIC_API_KEY` as a credential, never engine logic. `null` unless
  enabled, an API key is configured, and the object has flags; every
  non-null note is implicitly `needs_human_review`, same contract as
  SQLGlot's LLM fallback. `lakebridge_rollup.csv` gets a matching
  `compatibility_notes_generated` aggregate row.
- **Supplementary catalog facts** — gathered directly by
  `source_exporter.py`'s own live `pyodbc` connection (never from the
  Analyzer report, which doesn't cover any of this), so these are
  populated independently of whether the `analyze` step itself succeeds:
  - `server_instance.json` — one `ServerInstanceEntity`
    (`SERVERPROPERTY(...)` / `sys.dm_os_sys_info` / `sys.configurations`),
    server-scoped, or `null` if unavailable.
  - `table_features.json` — one `TableFeatureEntity` per table
    (temporal/memory-optimized/CDC/change-tracking/partitioning/
    compression flags), joinable by `(schema, name)`.
  - `procedure_parameters.json` — one `ProcedureParameterEntity` per
    `sys.parameters` row for every stored procedure/function in the
    source database; `name` is the containing proc/function's name, not
    the parameter's own name (that's `parameter_name`). Standalone rather
    than merged into the object inventory above, since the Analyzer-report-
    derived `LakebridgeObjectRef` has no parameters field.
  - `server_security.json` — `{"server_principals": [...],
    "server_permissions": [...]}`, both server-scoped
    (`sys.server_principals`/`sys.server_role_members`/
    `sys.server_permissions`).
  - `linked_servers.json` — `sys.servers` filtered to linked servers,
    with `provider_string_redacted` defensively scrubbed of any
    `password=`/`pwd=` substring.

  In `fixture` mode these are populated from plausible, clearly-marked
  synthetic values (`server_instance.json`'s `edition`/`product_version`
  are tagged `[FIXTURE DATA]`) parsed via plain regex out of the same
  `fixtures/sql/ddl_sample.sql` this engine already stages for the
  Analyzer — never via `fixtures/mock_catalog.py` (that module is SQLGlot
  Discovery's own sqlglot-AST fixture parser).
- `lakebridge_rollup.csv` — flat counts, including one row per
  supplementary-metadata category above and one row per distinct
  `compatibility_flags` value found across the object inventory (e.g.
  `compatibility_flag,PIVOT,3`)
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
- `comparison_report.csv` — one row per object category, both engines'
  counts and the difference
- `comparison_report.md` — human-readable report: engine run status
  (success/partial/failed/not_run, duration, error/warning counts), the
  category table, a best-effort sample of object names found by one engine
  but not the other, a dependency-edge breakdown by relationship type and
  discovery method, and a "Generated vs. native categories" section (see
  below)

Safe to run even if one or both engines haven't run yet or failed —
missing output is reported as `not_run`/`failed` rather than raising.
Name-based "found by one engine but not the other" matching is
best-effort (see `discovery_comparison/comparator.py`'s docstring) —
**counts are the reliable signal**, name-level matching is a bonus, not a
guaranteed-precise join.

**Category coverage stays automatically synchronized with both engines.**
`comparator.py` builds the category table in two passes: an explicit,
name-matched list (`_CATEGORY_SPECS`) for categories where both engines
write a JSON file of named objects with a compatible shape (tables, views,
procs, functions, triggers, synonyms, SSIS packages, schemas, sequences,
UDTs, XML schema collections, agent jobs, CLR assemblies, indexes,
constraints); then an **auto-sync pass** that reads the `object_type`
column of each engine's own rollup CSV (`discovery_rollup.csv` /
`lakebridge_rollup.csv`) and adds a count-only comparison row for every
`object_type` not already covered above. This means a brand-new rollup row
added to either engine's `output_writer.py` in the future appears in the
next comparison run automatically — no edit to `discovery_comparison/`
required.

**Generated vs. native categories.** A handful of categories are produced
*by the discovery engines themselves*, not by any single SQL Server
catalog object — the comparison report labels these explicitly so a count
difference there isn't mistaken for the same kind of discrepancy as, say,
a table count (which SSMS *can* verify directly with one query):

| Category | Native SQL Server object? | What it actually is |
|---|---|---|
| `database_summary` | No | A generated rollup, assembled from several catalog views (`sys.databases`, `sys.tables`, `sys.indexes`, `sys.foreign_keys`, `sys.database_principals`, `DATABASEPROPERTYEX`, ...). There is no single `sys.database_summary` view — the shape can only be re-derived by composing the same views by hand, not queried directly. |
| `data_quality_summary` | No | Computed entirely by the discovery engine from metadata it already collected (`sys.tables`/`sys.columns`/`sys.indexes`/`sys.foreign_keys`/`sys.triggers`/...). Not a native object; approximately reproducible by re-deriving the same per-table/per-column heuristics, never queryable as-is. |
| `unsupported_objects` | No — and the two engines mean different things by it | SQLGlot: objects whose own T-SQL text failed to parse or only partially parsed (`parse_status`/`unresolved_reason`) — a **parser-capability** signal. Lakebridge: objects the Databricks Analyzer's own report flags as unconvertible — a **migration-feasibility** signal. SQL Server has no catalog concept of "unsupported for migration" in either sense. |
| `warnings` | No | SQLGlot: per-object parser/lineage degradation messages (same condition as `unsupported_objects`, phrased as text). Lakebridge: per-pipeline-stage operational messages (missing directory, unreadable file, failed probe connection) — a different granularity. Neither is SQL Server metadata. |

These four notes are also emitted at runtime into
`comparison_report.md`'s "Generated vs. native categories" section
(`discovery_comparison/comparator.py`'s `GENERATED_ARTIFACT_NOTES`), scoped
to only the categories the current run actually produced.

## Adding a third Discovery engine later

Each engine is a self-contained package with the same shape (`config.py`,
`logging_setup.py`, `schema.py`, an `orchestrator.py` exposing
`run_discovery()`, and its own output directory). To add another engine:
write a new package following that shape, add it as a third independent
step in `run_all_discovery.py` (wrapped in its own try/except, same as the
other two), and extend `discovery_comparison/comparator.py`'s category
loop to also read its output directory. No existing engine's code changes.

---

# Assessment Phase

Everything above this line is Discovery (inventory + dependency graph,
does no scoring). This section covers the **Assessment phase**
(`assessment/`), which reads the **sqlglot Discovery engine's** manifest
(`./output/discovery_manifest.json`) only — never Lakebridge's
(`./output_lakebridge/`), which is a structurally different schema (see
"Multi-engine Discovery" above) — and turns it into a migration-planning
deliverable: per-object complexity/effort, a risk register, a
dependency-ordered migration wave plan, a data-readiness rollup, and a
security/permissions migration note.

Reads Discovery's manifest as plain JSON, never by importing `autovista`'s
internal dataclasses, so the two phases stay independently runnable —
Assessment can be re-run against a re-generated Discovery manifest without
touching Discovery, and vice versa.

## Running Assessment

```bash
python3 -m autovista.orchestrator      # produces ./output/discovery_manifest.json first, if not already run
python3 -m assessment.orchestrator     # reads it, writes ./output_assessment/
```

Override the input/output paths (e.g. to assess a different Discovery run)
via `ASSESSMENT_INPUT_MANIFEST` / `ASSESSMENT_OUTPUT_DIR`. See
`assessment/config.py` for every override, including the effort-hours-per-
tier rubric (`ASSESSMENT_HOURS_LOW/MEDIUM/HIGH/CRITICAL`) and the
complexity-tier score thresholds — both are stated *assumptions*, not
measured facts; replace them with your own team's actuals as soon as you
have a few objects' real remediation time.

## What each module produces

- **`complexity_scorer.py`** — scores every table, view, stored procedure,
  function, trigger, and SSIS embedded-SQL task into a `Low/Medium/High/
  Critical` tier + an estimated-hours figure, from signals Discovery
  already computed (LOC, reference breadth, `compatibility_flags`, dynamic
  SQL usage, parse health, dependency fan-in/out for code objects; DDL
  feature richness — temporal/CDC/memory-optimized/computed columns/etc. —
  for tables). Every score carries `scoring_reasons` so a reviewer can see
  *why*, not just the number.

  One data-shape wrinkle worth knowing: SQL Server represents a single
  multi-event trigger (`CREATE TRIGGER ... FOR INSERT, UPDATE, DELETE`) as
  one row per event, and Discovery's extractor carries that straight
  through as multiple same-named `TriggerEntity` rows — confirmed against
  this build's own AdventureWorks2022 run (13 trigger rows for 11 distinct
  trigger definitions; also the root cause of the "13 vs 10 triggers"
  sqlglot/Lakebridge mismatch in `output_comparison/comparison_report.md`,
  since Lakebridge counts `sys.triggers` directly). `complexity_scorer.py`
  merges same-named trigger rows before scoring so effort isn't counted
  2-3x for one real trigger.

- **`risk_register.py`** — one finding per unresolved/partially-parsed
  object (from Discovery's own `unsupported_objects`), per compatibility-
  flagged object (severity mapped per flag — e.g. `LINKED_SERVER`/
  `XP_CMDSHELL` are Critical, `PIVOT` is Low, since Databricks SQL supports
  it natively), per CLR assembly, and per linked server.

- **`migration_wave_planner.py`** — orders tables/views/procs/functions/
  triggers into migration waves via topological leveling of the dependency
  graph (Tarjan SCC for cycle detection — mutually-dependent objects
  collapse into one wave, flagged `has_circular_dependency`), so wave N
  never depends on anything not already migrated in wave N-1 or earlier.

- **`data_readiness.py`** — rolls up Discovery's `data_quality_summary`
  (metadata-only, no new queries) into severity + a Databricks/Delta Lake
  migration recommendation per signal (e.g. CDC-enabled tables → High,
  needs a CDC ingestion re-design; heap tables → Low, no clustered-index
  concept on Databricks).

- **`security_review.py`** — privileged server logins, linked-server
  credential relationships, unsafe CLR assemblies, and high-privilege
  permission grants, framed around what needs deliberate re-design for
  Unity Catalog's principal/grant model (not a security audit).

- **`output_writer.py`** — per-category JSON + an aggregate
  `assessment_manifest.json` + CSV rollups (`assessment_rollup.csv`,
  `risk_register.csv`, `object_complexity.csv`, `migration_waves.csv`) +
  a human-readable `assessment_report.md`.

## Project layout addition

```
assessment/
  schema.py                   # output contract dataclasses
  config.py                   # env-var-driven config incl. the effort/threshold rubric
  dependency_index.py         # shared fan-in/fan-out adjacency view over Discovery's dependencies[]
  complexity_scorer.py        # per-object Low/Medium/High/Critical + estimated hours
  risk_register.py            # unresolved objects + compatibility flags + CLR + linked servers
  migration_wave_planner.py   # dependency-ordered wave plan (Tarjan SCC + leveling)
  data_readiness.py           # data_quality_summary -> readiness findings
  security_review.py          # security/permissions migration notes
  summary.py                  # executive-summary rollup
  output_writer.py            # JSON + CSV + Markdown report
  orchestrator.py             # wires it all together; `python -m assessment.orchestrator`

tests/test_assessment_*.py    # unit tests per module + one end-to-end orchestrator test
```

---

# Lakebridge Assessment (comparing our scoring vs. Lakebridge's own)

`lakebridge_assessment/` is a second, independently-coded Assessment
package, read `./output_lakebridge/lakebridge_manifest.json` (the
Lakebridge Discovery engine's output) and producing
`./output_lakebridge_assessment/` -- the same shape of deliverable as
`assessment/` (complexity tiers, risk register, migration waves, data
readiness, security notes), so the two can be compared side by side.

**Key difference from `assessment/`:** while building this, we found that
Lakebridge's own Analyzer report already carries a native per-object
complexity rating (`LakebridgeObjectRef.complexity`, e.g. `LOW`/`MEDIUM` --
see `lakebridge_discovery/report_parser.py`'s `_COMPLEXITY_KEYS`), already
present in `lakebridge_manifest.json` from a real (`run_mode: live`)
Analyzer run. Rather than re-deriving complexity with our own heuristic
against Lakebridge's data, `lakebridge_assessment/complexity_mapper.py`
trusts Lakebridge's own rating as-is -- this is meant to be genuinely
"Lakebridge's assessment," not our logic re-applied to their data. Objects
the Analyzer report gave no complexity value at all (e.g. schemas,
synonyms, CLR assemblies in this project's own output) are skipped, not
guessed at -- see `AssessmentSummary.objects_without_native_complexity`.

Same non-negotiable caveat as the rest of the Lakebridge integration in
this repo: the Analyzer's report schema is undocumented at the field
level, so `mapping_verified`/`mapping_notes` from the Discovery manifest
are carried straight through into this package's own output -- don't
trust the complexity ratings more than you'd trust any other Lakebridge-
sourced field until that mapping is independently verified.

Independent of `assessment/` by design (same "engines never share code"
convention `lakebridge_discovery/` already follows relative to
`autovista/`) -- same field names/shapes, retyped rather than imported, so
the two output directories are directly comparable by hand. No automatic
comparison module was built (by choice, for now) -- diff
`output_assessment/` vs. `output_lakebridge_assessment/` yourself (the
Markdown reports or the `*_rollup.csv`/`object_complexity.csv` files are
the easiest starting point).

## Running it

```bash
python3 -m lakebridge_discovery.orchestrator   # produces ./output_lakebridge/lakebridge_manifest.json first, if not already run
python3 -m lakebridge_assessment.orchestrator  # reads it, writes ./output_lakebridge_assessment/
```

Override paths via `LAKEBRIDGE_ASSESSMENT_INPUT_MANIFEST` /
`LAKEBRIDGE_ASSESSMENT_OUTPUT_DIR`; the effort-hours-per-tier rubric
(`LAKEBRIDGE_ASSESSMENT_HOURS_*`) defaults to the same values as
`assessment/config.py`'s rubric so the two reports' hour totals are
comparable out of the box -- override independently if you want to test
the two rubrics diverging.

On this project's own AdventureWorks2022 run: sqlglot-based `assessment/`
estimated **386 hours** across 122 objects; Lakebridge-based
`lakebridge_assessment/` estimated **320 hours** across the same 122
objects, using Lakebridge's own tiers (103 Low / 19 Medium vs. sqlglot's
computed 89 Low / 35 Medium / 1 High). That gap is itself a useful signal
worth digging into by hand before trusting either number outright.

## Project layout addition

```
lakebridge_assessment/
  schema.py                   # output contract dataclasses (retyped from assessment/schema.py, not shared)
  config.py                   # env-var-driven config; same default hours rubric as assessment/config.py
  dependency_index.py         # case-insensitive fan-in/fan-out index (Lakebridge's own casing is inconsistent)
  complexity_mapper.py        # normalizes Lakebridge's OWN native complexity rating -- no scoring formula
  risk_register.py            # unsupported objects + compatibility flags + CLR + linked servers
  migration_wave_planner.py   # same Tarjan SCC + leveling algorithm, case-insensitive object matching
  data_readiness.py           # data_quality_summary + table_features -> readiness findings
  security_review.py          # security/permissions migration notes
  summary.py                  # executive-summary rollup
  output_writer.py            # JSON + CSV + Markdown report
  orchestrator.py             # wires it all together; `python -m lakebridge_assessment.orchestrator`

tests/test_lakebridge_assessment_*.py   # unit tests per module + one end-to-end orchestrator test
```

---

# LLM Assessment (a third, fully independent method)

`llm_assessment/` is a third way of scoring migration complexity, alongside
`assessment/`'s fixed point-formula heuristic and `lakebridge_assessment/`'s
use of Lakebridge's own native rating. Instead of either of those, this
phase asks an LLM (Claude) to judge each object's complexity tier directly.

**Fully self-contained -- zero imports from `assessment/`, `lakebridge_assessment/`,
or `autovista/`.** Every module this package needs (schema, dependency
index, risk register, migration wave planner, data readiness, security
review, summary, output writer, even its own `.env` loader) is its own
copy under `llm_assessment/`, not an import of the equivalent module
elsewhere in this repo. This was originally built reusing `assessment/`'s
deterministic modules directly (since both read the same sqlglot
manifest, not two independent engines being compared against each other),
but was deliberately decoupled on request so this package keeps working
even if `assessment/` and `lakebridge_assessment/` are deleted later --
its only real dependency is the Discovery manifest JSON *file* already on
disk, not any package that produced it. Same field names/shapes as
`assessment/schema.py` are kept (retyped, not imported) purely so the
three tools' output stays comparable by eye -- the same "independent copy,
comparable shape" convention `lakebridge_discovery/` already uses relative
to `autovista/`.

**Deliberately metadata-only** for the complexity judgment (per this
build's own scope decision): the LLM is given the same structured signals
the heuristic scorer computes -- LOC, reference breadth,
`compatibility_flags`, dynamic SQL usage, parse health, dependency
fan-in/fan-out -- never raw SQL source text. That keeps this phase working
directly off the sqlglot Discovery manifest with no extra source-export
step, and isolates the comparison to "does an LLM judge the same facts
differently than our formula."

Hard rules, same non-negotiable contract as `autovista/llm_fallback_extractor.py`
and `autovista/compatibility_remediation.py`:
- Never a source of truth -- every tier is explicitly labeled "LLM
  judgment" in `scoring_reasons`, plus a mandatory disclaimer in
  `manifest.warnings` and at the top of the Markdown report.
- Hard cap on LLM calls per run (`LLM_ASSESSMENT_MAX_OBJECTS_PER_RUN`,
  default 200) -- objects beyond the cap are excluded, not guessed at.
- No API key configured -> every object is left unscored, not guessed.
- One object's LLM failure (network error, malformed JSON, an
  unrecognized tier string) never fails the run -- that object is simply
  excluded and counted under the run's `failed` stat.

## Databricks infra-sizing recommendations (new, deterministic -- no LLM call)

`infra_sizing.py` looks at Discovery's database/table size metadata
(`databases[].size_mb`, `tables[].size_mb`/`row_count`) and produces
concrete Databricks infrastructure recommendations: a SQL Warehouse
t-shirt size, an ingestion/migration cluster size, and a
partitioning-vs-Liquid-Clustering call per table. This is deterministic
(threshold lookups against known specs), not an LLM judgment call --
sizing a warehouse is a lookup, not something LLM reasoning adds value to,
and keeping it deterministic means it's free and instant to re-run.

Grounded in Databricks' own published guidance, fetched directly from
their docs while building this (not from training-data memory, since
sizing tables drift):
- SQL warehouse t-shirt sizes (2X-Small through 5X-Large) and their AWS
  reference driver/worker instance types -- [SQL warehouse sizing, scaling, and queuing behavior](https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-behavior).
- "Don't partition tables under 1TB; use Liquid Clustering instead,"
  Liquid Clustering being Databricks' current (2026) default
  recommendation for all new Delta tables -- [When to partition tables](https://docs.databricks.com/aws/en/tables/partitions),
  [Use liquid clustering for tables](https://docs.databricks.com/aws/en/tables/clustering).

The GB/TB-to-warehouse-size thresholds themselves are this module's own
assumption, not a Databricks-published rule -- real warehouse sizing
depends on query concurrency/complexity, which static Discovery metadata
can't see at all. Treat every recommendation as a capacity-planning
starting point to validate once you have real query patterns, not a
committed spec (see `infra_sizing.py`'s module docstring).

## A real bug found and fixed while building this

Haiku 4.5 wrapped its JSON responses in a markdown code fence
(` ```json ... ``` `) despite the system prompt's explicit "respond ONLY
with JSON, no prose" instruction, which broke `json.loads()` on every
single call in the first live smoke test. Fixed in `llm_client.py` by
stripping a leading/trailing code fence before parsing -- don't rely on
prompt wording alone to guarantee bare JSON from a real model.

## Running it

```bash
python3 -m autovista.orchestrator     # produces ./output/discovery_manifest.json first, if not already run
python3 -m llm_assessment.orchestrator  # reads it, writes ./output_llm_assessment/
```

Requires `ANTHROPIC_API_KEY`. Defaults to `claude-haiku-4-5-20251001`
(cheap/fast, appropriate for a bulk one-call-per-object classification
task) -- override via `LLM_ASSESSMENT_MODEL` for a stronger model at
higher cost/latency. Calls are made sequentially (not batched), so a
~120-object estate takes roughly 15-20 minutes end to end at Haiku's
typical latency for this prompt. Effort-hours-per-tier rubric
(`LLM_ASSESSMENT_HOURS_*`) defaults to 0.5/2/6/8 hours for Low/Medium/
High/Critical -- this package's own copy of that assumption, independent
of `assessment/config.py`'s rubric (see config.py).

## Result on this project's own AdventureWorks2022 run

| | `assessment/` (heuristic) | `lakebridge_assessment/` (Lakebridge native) | `llm_assessment/` (Claude Haiku) |
|---|---|---|---|
| Objects scored | 122 | 122 | 122 (0 failed) |
| Tier breakdown | 89 Low / 35 Medium / 1 High | 103 Low / 19 Medium | 97 Low / 15 Medium / 7 High / 3 Critical |
| Total estimated hours | 386 | 320 | 144.5 (using this package's own 0.5/2/6/8 rubric -- not directly comparable to the other two columns' 2/6/16/32 rubric) |

Databricks infra-sizing recommendations for this same estate (272 MB, 71
tables, largest table 30.55 MB): **2X-Small SQL Warehouse**, a
**single-node ingestion cluster** for the one-time migration load, and
**no partitioning needed anywhere** (every table is far below the 1TB
threshold) -- see `output_llm_assessment/infra_sizing.json` for the full
rationale on each.

The most interesting divergence in the first full run: the LLM rated
every trigger High or Critical, while the other two tools mostly rated
them Low/Medium. Its rationale, verbatim from one of the Critical-rated
triggers: *"Trigger objects have no direct equivalent in Databricks/Delta
Lake. Delta Lake does not support triggers for automated DML actions...
would require manual re-architecture using alternative patterns."* That's
a real, correct architectural fact neither the heuristic formula nor
Lakebridge's native rating captured -- both of those score off
syntax-level signals (LOC, named constructs), not "does this category of
object exist at all on the target platform." Worth treating as a genuine
finding to investigate, not just an LLM being pessimistic.

## Project layout addition

```
llm_assessment/
  schema.py                 # output contract dataclasses (own copy, incl. InfraSizingRecommendation)
  config.py                 # env-var-driven config; own EffortRubric + own .env loader, no cross-package imports
  dependency_index.py       # own fan-in/fan-out adjacency index (case-sensitive -- sqlglot manifest only)
  llm_client.py              # self-contained Anthropic client wrapper (markdown-fence stripping, request timeout)
  complexity_scorer.py       # builds per-object metadata descriptions, calls the LLM, parses tier/confidence/rationale
  risk_register.py           # own copy: unresolved objects + compatibility flags + CLR + linked servers
  migration_wave_planner.py  # own copy: Tarjan SCC + leveling
  data_readiness.py          # own copy: data_quality_summary -> readiness findings
  security_review.py         # own copy: security/permissions migration notes
  infra_sizing.py            # NEW: DB/table size -> Databricks SQL Warehouse/cluster/partitioning recommendations
  summary.py                 # own copy: executive-summary rollup
  output_writer.py           # own copy: JSON + CSV + Markdown report (now includes an infra-sizing section)
  orchestrator.py            # wires it all together; `python -m llm_assessment.orchestrator`
  logging_setup.py           # per-object progress logging (a long live run needs this to be observable)

tests/test_llm_assessment_*.py   # unit tests; complexity_scorer/orchestrator use a fake in-memory LlmClient -- never a real API call in the test suite
```
