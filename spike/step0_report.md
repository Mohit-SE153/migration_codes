# Step 0 — Extraction Method Comparison (sqlglot vs. Databricks Lakebridge vs. LLM fallback)

**Status: executed spike, not a desk review.** sqlglot and the XML-parsing path were run
against a purpose-built synthetic sample environment (`fixtures/`) and the numbers below
are measured, not estimated. Databricks Lakebridge could not be installed or licensed in
this build environment (no network egress to Databricks tooling, no workspace), so its row
is sourced from published documentation and flagged accordingly — **do not treat it as
validated to the same standard as the other two rows.** The LLM fallback's structured
extraction path was built and its I/O contract exercised, but no live call was made against
the Anthropic API (no key configured in this sandbox); its accuracy claim below is
supported by a manual dry-run, not automated measurement — also flagged.

This asymmetry is itself the headline finding: **direct catalog queries and sqlglot are
cheap to validate and should be trusted first; anything claiming to replace them (or to
replace human review) needs its own spike against your real estate before you rely on it.**

## Live validation addendum (post-spike, against a real SQL Server 2022 instance)

Everything above was written before any live SQL Server was reachable. It since became
available (a real SQL Server 2022 instance running `AdventureWorks2022`, `CompanyDB`,
`SchoolDB`), and running the pipeline against it — not synthetic fixtures — surfaced real
bugs the fixture-only spike couldn't have caught, exactly as the "validate before trusting"
principle above predicts. All fixes below are in `autovista/` and covered by new regression
tests in `tests/test_sql_lineage_parser.py`.

**Direct metadata queries: fully validated, no changes needed.** Databases, tables, columns,
row counts, sizes, stored procs, triggers, and foreign keys all extracted correctly against
AdventureWorks2022 (71 tables, 10 procs, 20 views, 13 triggers) on the first real run.

**sqlglot lineage parsing: 3 real, previously-undetected bugs found and fixed.** All three
were invisible against the synthetic fixture set because it happened not to contain these
constructs — a reminder that a spike's coverage is bounded by what's in the sample:

1. **`OPTION (MAXRECURSION 25)`** (a common T-SQL query hint on recursive CTEs, irrelevant to
   lineage) isn't supported by sqlglot's tsql grammar and made the *entire* statement raise a
   parse error, wrongly flagging 4 procs (`uspGetBillOfMaterials`, `uspGetEmployeeManagers`,
   `uspGetManagerEmployees`, `uspGetWhereUsedProductID`) as `unresolved` when they were
   perfectly parseable otherwise. Fixed by stripping `OPTION (...)` clauses before parsing
   (replaced with `;` to keep the preceding statement explicitly terminated — see next bug).
2. **CTE self-references leaking into `referenced_tables`.** A recursive CTE re-referencing
   itself with a fresh alias (`FROM [BOM_cte] cte`) was reported as a table named `BOM_cte` —
   the existing bare-alias filter only excluded *unaliased* self-references, not aliased
   ones. Fixed by excluding CTE names unconditionally, not just when unaliased.
3. **`BEGIN TRY`/`BEGIN CATCH` silently dropping every table reference inside — the most
   serious finding.** sqlglot's tsql grammar has no concept of TRY/CATCH at all: `BEGIN TRY`
   misparses as a bogus column alias, and everything after it degrades into opaque, unsearched
   `Command` nodes. This is a **worse failure mode than `unresolved`**: `uspLogError` and two
   other real procs were returning `parse_status: sqlglot` with a confidently *empty* table
   list — no error, no flag, nothing distinguishing "genuinely no tables" from "we lost
   track." TRY/CATCH is extremely common T-SQL error-handling and this would have silently
   produced wrong (incomplete) dependency graphs at scale. Fixed two ways: (a) TRY/CATCH
   markers are stripped and both bodies flattened into the outer block before parsing (safe
   for lineage purposes specifically, since TRY/CATCH is pure control flow with no bearing on
   which tables are touched); (b) as a backstop for constructs even this doesn't fix (e.g. a
   nested `IF/BEGIN/END` inside a TRY block, found in the same `uspLogError`), any remaining
   `Command` node now triggers a best-effort regex fallback *and* unconditionally sets
   `unresolved_reason` — so a partial result is now clearly flagged instead of presented as
   complete. (One regex-fallback follow-on bug also fixed: `CONTAINSTABLE`/`FREETEXTTABLE`
   full-text-search functions were being misread as table names by the new regex fallback.)

After all three fixes: **10/10 real stored procedures and 20/20 real views resolve via
sqlglot, zero silent failures** (verified by walking every proc/view definition on the
instance, not just the four originally flagged).

**SSISDB catalog extraction: one real bug fixed, plus a correctly-handled negative case.**
The catalog queries (`catalog.projects`, `catalog.packages`, `catalog.get_project`) were
unqualified, which only resolves if the connection's default database happens to be `SSISDB`
— it isn't (the connection is scoped to the source data database being discovered). Fixed by
qualifying every catalog reference with `SSISDB.`. Separately, this instance has no `SSISDB`
database at all (confirmed via `sys.databases`), so SSIS extraction now fails with a correct,
informative "Invalid object name 'SSISDB.catalog.projects'" error — cleanly isolated per the
error-isolation design (the rest of the run still completed: `scanned=101 failed=1`), not a
pipeline bug. Also fixed: `get_package_xml` wasn't receiving the SSISDB *folder* name (only
project name), which `catalog.get_project` requires — folder is now threaded through end to
end and stored on `PackageEntity.folder`.

**Live connectivity: two real environment-specific bugs fixed.** (1) ODBC Driver 18 defaults
to strict TLS certificate validation and rejected this instance's self-signed cert with error
`08001`; added `AUTOVISTA_SQL_TRUST_SERVER_CERT` (default `false`, opt-in per-environment).
(2) `list_databases()` returned *every* database on the server (3, in this case) with
`table_count`/`proc_count`/`view_count` hardcoded to `0` for all of them — misleading, since
only one database is ever actually scanned per run. Fixed to scope to the connected database
only and backfill real counts from what was actually extracted.

**Net effect on this report's own recommendation:** unchanged, but now on firmer footing.
The hybrid architecture holds; what changed is confidence in the sqlglot row specifically —
it went from "10/11 + 3/3 on a hand-built fixture set" to "10/10 + 20/20 on a real instance,
with three previously-invisible failure modes found and closed." The finding that most
generalizes: **a parser returning a confident, empty result is a worse failure mode than one
that raises** — worth checking for in any future extraction-method evaluation, not just this
one.

## Sample environment used for the spike

Synthetic "SalesDW" pilot database + 5 SSIS packages, built specifically to exercise every
construct called out in the build prompt (dynamic SQL, Script Task, Execute Package Task
chains, multi-table joins, ForEach loops):

- 21 tables (15 `dbo` + 6 `staging`), 3 views, 11 stored procedures, 1 trigger, 2 SQL Agent
  jobs — see `fixtures/sql/ddl_sample.sql`
- 5 `.dtsx` packages of increasing complexity — `fixtures/dtsx/*.dtsx`:
  1. `Pkg_LoadCustomers` — single data flow, one upstream Execute SQL Task (low)
  2. `Pkg_LoadOrders` — Sequence container, Lookup component, multi-table join source (medium)
  3. `Pkg_LoadOrderDetails` — ForEach Loop, a Script Task (C#), and a dynamic-SQL Execute SQL
     Task calling `usp_DynamicReportBuilder` (high — deliberately includes the two construct
     types no static parser can resolve)
  4. `Pkg_ArchiveOldData` — Execute SQL Task calling a parameterized proc, File System Task (medium)
  5. `Pkg_Master` — pure orchestrator, 4 Execute Package Task nodes with success/completion
     precedence branching (orchestration-level)

Reproduce the numbers below with `python3 spike/spike_runner.py`.

## Coverage matrix (✅ = extracts natively, ⚠️ = partial/indirect, ❌ = out of scope)

| Artifact type | sqlglot | Lakebridge (docs-sourced) | LLM extraction |
|---|:---:|:---:|:---:|
| Table/column DDL, row counts, sizes | ❌ (not its job — always direct query) | ⚠️ profiles source but treats sizing as ground-truth-via-query too | ❌ (never used for this — hard rule) |
| Stored proc / view → table lineage (static SQL) | ✅ measured 10/11 procs, 3/3 views | ✅ claimed (SQL dialect coverage is the tool's core strength) | ⚠️ can do it, but adds cost/latency for no benefit over sqlglot |
| Dynamic SQL (`sp_executesql`, string-built queries) | ❌ correctly flagged `unresolved`, never guesses | ⚠️ unclear — docs don't specify dynamic-SQL resolution; unvalidated | ✅ can use call-site context sqlglot can't see (see dry-run below) |
| `.dtsx` control-flow structure (tasks, containers, precedence) | ❌ no XML concept at all | ⚠️ claimed ETL/SSIS coverage, but docs flag it as narrower than SQL coverage — **not validated for SSIS specifically in this spike** | ✅ can read raw XML, but a deterministic XML parser is strictly better (cheaper, deterministic) |
| SSIS connection managers / variables | ❌ | ⚠️ unvalidated | ✅ but same as above — XML parsing wins on cost/determinism |
| OLE DB Source/Destination / Lookup embedded SQL | ✅ once extracted from XML, sqlglot parses it like any other SQL string | ⚠️ unvalidated | ✅ but redundant once XML parsing hands it a bare SQL string |
| Execute Package Task → package dependency | ❌ | ⚠️ unvalidated | ✅ but XML gives an exact, deterministic answer (`ExecutePackageTaskData/@PackageName`) |
| Script Task source code (C#/VB) | ❌ not SQL | ❌ not mentioned as in scope | ✅ only method that can attempt this at all |
| Cross-system interdependency / complexity scoring | ❌ out of scope for this phase anyway | ✅ this is Lakebridge's headline feature | ❌ out of scope for this phase |
| Output format | AST → programmatic extraction | Excel + JSON (per docs) | Free text, must be schema-constrained |

## Accuracy spot-check (measured, from `spike/spike_results.json`)

**sqlglot lineage parsing — 11 stored procedures, 3 views:**

| Result | Count | Detail |
|---|---|---|
| Correctly resolved (`parse_status: sqlglot`) | 10/11 procs, 3/3 views | Table + proc references extracted with zero false positives after fixing two AST edge cases (see below) |
| Correctly flagged unresolved | 1/11 procs | `usp_DynamicReportBuilder` — builds `@TableName` into a string and calls `sp_executesql`; sqlglot correctly refuses to guess the table name rather than fabricating one |

Two real bugs were found and fixed during the spike, which is exactly why this spike was
worth running before committing to the architecture:

1. `EXEC dbo.usp_Foo` parses into a dedicated `exp.Execute` AST node, not a plain
   `exp.Table` — naive `find_all(exp.Table)` misclassified proc calls as table references.
   Fixed by special-casing `exp.Execute.this`.
2. `UPDATE i SET ... FROM dbo.Inventory i JOIN ...` (T-SQL's alias-target UPDATE syntax)
   produces a bare `Table(this=i)` node for the update target that is *not* the same AST
   node as the aliased `Inventory i` table — naive extraction picked up `"i"` as a phantom
   table. Fixed by collecting `TableAlias` names first and filtering bare alias references.

Both are the kind of thing that "sqlglot handles T-SQL out of the box" glosses over — this
is a **general-purpose SQL parser with T-SQL support**, not a T-SQL specialist, and its
edge-case behavior needs verification per dialect construct, not just per statement type.

**XML parsing — 5 packages:**

| Result | Count | Detail |
|---|---|---|
| Packages fully parsed | 5/5 | Tasks, containers (with correct parent nesting), connection managers, variables, precedence constraints, embedded SQL text all extracted |
| Script Tasks correctly flagged unparseable | 1/1 | `Apply Regional Discount Rules` in `Pkg_LoadOrderDetails` — C# source captured but not interpreted, `unparseable_body=True` set for downstream LLM/human routing |
| Execute Package Task → package edges resolved | 4/4 | `Pkg_Master`'s four child-package references, exact string match, no ambiguity |

**LLM fallback — manual dry-run (not an automated call; no live API key in this sandbox):**

To illustrate the LLM fallback's actual differentiated value (not just "can read text"),
here is what a real Claude call against `AUTOVISTA_LLM_MODEL=claude-sonnet-5` would very
plausibly return for the two objects sqlglot/XML parsing correctly punted on — reasoned
through manually against `llm_fallback_extractor.EXTRACTION_SYSTEM_PROMPT`:

- **`usp_DynamicReportBuilder`** (dynamic SQL): sqlglot sees only the isolated proc body and
  correctly cannot resolve `@TableName`. An LLM call with a wider context window *can* be
  handed the full discovery manifest and cross-reference call sites — in this fixture set,
  `Pkg_LoadOrderDetails`'s "Dynamic Region Report" task is the only observed caller, passing
  `@TableName = N'OrderDetails'`. A well-grounded LLM response would be:
  `referenced_tables: ["dbo.OrderDetails"], confidence: "medium", notes: "table name is a
  runtime parameter; inferred from the single observed call site — confirm no other callers
  exist before treating this as complete."` This is genuinely useful and something no static
  parser can produce alone, but it is **conditional on the caller inventory being complete**,
  which is exactly why it's flagged `llm_inferred` + `needs_human_review=True` rather than
  accepted as fact.
- **`Apply Regional Discount Rules` Script Task**: the visible C# calls an undefined
  `ApplyTieredDiscount(region)` helper with no visible table/column reference — an honest LLM
  response should return `referenced_tables: [], confidence: "low", notes: "modifies the
  pipeline buffer via an undocumented helper method; Script Tasks can also issue hidden
  ADO.NET calls not shown in Main() — recommend a human pull the full script project before
  this is trusted."` A *bad* LLM implementation would hallucinate a table name here to seem
  useful — the schema in `llm_fallback_extractor.py` forces a `confidence` field precisely so
  low-information cases surface as low-confidence instead of confident guesses.

**What this dry-run does NOT establish:** actual model output on real, messier production
Script Tasks/dynamic SQL (obfuscated string building, multi-hundred-line VB scripts,
nested dynamic SQL) may be far less clean than these two fixture cases. Before relying on
this path at scale, run the automated path (`AUTOVISTA_LLM_FALLBACK_ENABLED=true` +
`ANTHROPIC_API_KEY` set) against a larger real sample and re-measure — this report's LLM row
should be treated as "the mechanism works and the prompt/schema hold up to manual reasoning,"
not "validated accuracy at scale."

## Cost / speed / effort tradeoff at assumed scale

Estate size assumption (per the chosen "small pilot" scope): **~20 databases, ~400 tables,
~150 stored procedures, ~50 SSIS packages (~150 tasks total)**.

| Method | Speed | Effort to stand up | Cost at this scale |
|---|---|---|---|
| Direct metadata queries (`sys.*`/DMVs) | Fast — single batched pass per database, seconds to low minutes for 20 DBs | Low — the queries in `sql_metadata_extractor.py` are standard and stable across SQL Server versions | Free (just DB load; a read-only account with `VIEW SERVER STATE` avoids locking) |
| sqlglot lineage parsing | Fast — pure in-process parsing, no network; ~150 procs parse in well under a second | Low-medium — correct once the two AST edge cases above are handled; needs a small T-SQL-construct regression suite (see `tests/`) since sqlglot's dialect coverage has sharp edges | Free (CPU only) |
| XML parsing (`.dtsx`) | Fast — same profile as sqlglot, in-process | Low-medium — real SSDT-exported `.dtsx` has far more boilerplate than this build's representative fixtures; expect to spend time hardening the namespace/element handling against real exports before first production run | Free (CPU only) |
| Lakebridge | Unknown — not benchmarked here | Medium-high — separate tool, separate licensing/install path, output format (Excel+JSON) needs its own ingestion step to fold into this pipeline's manifest | Free to use per vendor docs, but has integration engineering cost this report can't size without a real install |
| LLM fallback (Claude Sonnet 5, current pricing: $3.00/$15.00 per MTok input/output, intro $2.00/$10.00 through 2026-08-31) | Slow relative to the others — network round trip per object, `AUTOVISTA_LLM_MAX_OBJECTS_PER_RUN` cap exists specifically to bound this | Low to build (already built here), but requires ongoing prompt/schema maintenance and **mandatory human review capacity** for every flagged object — that review time is the real cost, not the API bill | At this scale: if ~10–15% of the ~300 proc+task objects need fallback (≈35–45 objects, roughly matching the spike's 1/11 proc + 1/5-package-tasks rate), at ~1,000 input + ~300 output tokens per call that's **under $0.50 in API spend for the whole pilot estate** — the API cost is a rounding error; the real cost is human-reviewer time per flagged object |

**At enterprise scale** (the prompt's own example: 500 databases / 5,000 tables / 2,000
packages), the direct-query and sqlglot/XML paths scale roughly linearly and stay cheap
(minutes to low hours, still effectively free compute). LLM fallback cost also stays modest
in raw API terms (low hundreds of dollars even at a generous 15% fallback rate across
thousands of objects) — the actual constraint at that scale is human-review throughput, not
API budget, which is why `parse_status: llm_inferred` / `unresolved` and mandatory
`needs_human_review` flags are treated as first-class output, not an afterthought.

## Recommendation: hybrid, as anticipated

1. **Direct metadata queries are the only source of truth** for row counts, sizes, and
   object existence — `direct_metadata` parse_status, never inferred, confirmed by this
   spike's design (no other method was even considered for this).
2. **sqlglot is the primary lineage engine** for stored procs, views, and any embedded SQL
   text once extracted from `.dtsx` — validated at 10/11 + 3/3 in this spike, with the one
   failure being a *correct* refusal to guess, not a wrong answer. Ship it with the two AST
   fixes above and the regression tests in `tests/test_sql_lineage_parser.py`.
3. **A dedicated XML parser is the primary SSIS structure engine** — validated at 5/5 in
   this spike including containers, precedence constraints, and Execute Package Task edges.
   Lakebridge's SSIS coverage is explicitly called out in its own docs as narrower than its
   SQL coverage and was not independently validated here — **do not adopt Lakebridge for the
   SSIS-structure role without running this same kind of spike against a real Lakebridge
   trial first.** It may still be worth adopting later for its complexity-scoring output in
   the Assessment phase (out of scope for Discovery), but that's a separate decision.
4. **LLM fallback (Claude) is last-resort only**, gated behind an explicit config flag,
   capped per run, and every result is mandatorily flagged for human review
   (`llm_inferred`/`unresolved`, `needs_human_review=True`). It is not source of truth for
   anything Discovery is required to be authoritative about. Its value (shown in the manual
   dry-run above) is real but conditional on completeness of the surrounding manifest — which
   is exactly why the pipeline runs it *after* every other extractor has populated the
   manifest, not standalone.

This is the architecture implemented in `autovista/` — see the module list in the top-level
README.
