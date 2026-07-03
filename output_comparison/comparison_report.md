# Discovery Comparison Report

Generated: 2026-07-03T09:59:06.090539Z

Two independent Discovery engines analyzed the same source database. Neither engine consumed the other's output -- this report is the only place their results are read together.

## Engine run status

### SQLGlot Discovery

- Status: **success**
- Duration: 3.0s
- Started: 2026-07-03T15:28:52
- Finished: 2026-07-03T15:28:55
- Errors: 0
- Warnings: 0

### Lakebridge Discovery

- Status: **success**
- Duration: 5.99s
- Started: 2026-07-03T09:05:45.882557+00:00
- Finished: 2026-07-03T09:05:51.868310+00:00
- Errors: 0
- Warnings: 0
- Note: Lakebridge Analyzer's report schema is not publicly documented at the field level. This result is mapped defensively (tolerates missing/renamed fields) and has not been verified against a real Databricks workspace run. Re-validate this mapping against your first real report and update report_parser.py's key lookups if names differ.

## Category comparison

| Category | SQLGlot | Lakebridge | Difference | Matched (best-effort) |
|---|---:|---:|---:|---:|
| tables | 71 | 71 | 0 | 71 |
| views | 20 | 20 | 0 | 20 |
| stored_procedures | 10 | 10 | 0 | 10 |
| functions | 11 | 11 | 0 | 11 |
| triggers | 13 | 10 | 3 | 10 |
| synonyms | 0 | 0 | 0 | 0 |
| ssis_packages | 0 | 0 | 0 | 0 |
| dependencies | 108 | 0 | 108 | 0 |
| unsupported_objects | 4 | 0 | 4 | 0 |
