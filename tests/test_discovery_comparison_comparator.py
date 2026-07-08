"""
Tests for discovery_comparison.comparator's auto-sync rollup pass and the
unsupported_objects fix -- confirms every category present in either
engine's rollup CSV appears in the comparison without being hardcoded in
_CATEGORY_SPECS, and that unsupported_objects now reads each engine's real
unsupported_objects.json instead of a narrower bespoke recomputation.
"""
from __future__ import annotations

import csv
import json

from discovery_comparison.comparator import GENERATED_ARTIFACT_NOTES, _read_rollup_counts, build_comparison
from discovery_comparison.config import ComparisonConfig


def _write_csv(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=sorted({k for row in rows for k in row}))
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_read_rollup_counts_sums_multiple_rows_of_the_same_object_type(tmp_path):
    csv_path = tmp_path / "rollup.csv"
    _write_csv(csv_path, [
        {"object_type": "compatibility_flag", "object_name": "PIVOT", "count": "2"},
        {"object_type": "compatibility_flag", "object_name": "MERGE", "count": "1"},
        {"object_type": "database", "object_name": "SalesDW", "count": "1"},
    ])
    totals = _read_rollup_counts(csv_path)
    assert totals["compatibility_flag"] == 3
    assert totals["database"] == 1


def test_read_rollup_counts_missing_file_returns_empty_dict(tmp_path):
    assert _read_rollup_counts(tmp_path / "does_not_exist.csv") == {}


def test_build_comparison_auto_discovers_category_not_in_hardcoded_specs(tmp_path):
    """server_instance/database_summary/data_quality_summary/database_user/
    etc. are not in _CATEGORY_SPECS (no compatible JSON-list shape to
    name-match) -- confirms they still appear via the rollup auto-sync pass."""
    sqlglot_dir = tmp_path / "sqlglot"
    lakebridge_dir = tmp_path / "lakebridge"
    _write_csv(sqlglot_dir / "discovery_rollup.csv", [
        {"object_type": "server_instance", "object_name": "(all)", "count": "1"},
        {"object_type": "database_summary", "object_name": "(all)", "count": "1"},
        {"object_type": "database_user", "object_name": "(all)", "count": "4"},
    ])
    _write_csv(lakebridge_dir / "lakebridge_rollup.csv", [
        {"object_type": "server_instance", "object_name": "(all)", "count": "1"},
        {"object_type": "database_summary", "object_name": "(all)", "count": "1"},
        {"object_type": "database_user", "object_name": "(all)", "count": "4"},
    ])
    config = ComparisonConfig(sqlglot_output_dir=str(sqlglot_dir), lakebridge_output_dir=str(lakebridge_dir), output_dir=str(tmp_path / "out"))

    result = build_comparison(config)

    by_category = {c.category: c for c in result.categories}
    assert by_category["server_instance"].sqlglot_count == 1
    assert by_category["server_instance"].lakebridge_count == 1
    assert by_category["database_summary"].sqlglot_count == 1
    assert by_category["database_user"].sqlglot_count == 4


def test_build_comparison_new_future_category_needs_zero_code_changes(tmp_path):
    """The whole point of the auto-sync pass: a category neither engine had
    when this comparator was written still appears automatically."""
    sqlglot_dir = tmp_path / "sqlglot"
    lakebridge_dir = tmp_path / "lakebridge"
    _write_csv(sqlglot_dir / "discovery_rollup.csv", [{"object_type": "some_brand_new_category", "object_name": "(all)", "count": "5"}])
    _write_csv(lakebridge_dir / "lakebridge_rollup.csv", [{"object_type": "some_brand_new_category", "object_name": "(all)", "count": "3"}])
    config = ComparisonConfig(sqlglot_output_dir=str(sqlglot_dir), lakebridge_output_dir=str(lakebridge_dir), output_dir=str(tmp_path / "out"))

    result = build_comparison(config)

    by_category = {c.category: c for c in result.categories}
    assert by_category["some_brand_new_category"].sqlglot_count == 5
    assert by_category["some_brand_new_category"].lakebridge_count == 3


def test_build_comparison_does_not_duplicate_a_name_matched_category(tmp_path):
    """"table" (rollup singular) must resolve to the "tables" category
    _compare_category already produced, not create a second row."""
    sqlglot_dir = tmp_path / "sqlglot"
    lakebridge_dir = tmp_path / "lakebridge"
    _write_json(sqlglot_dir / "tables.json", [{"schema": "dbo", "name": "Orders"}])
    _write_json(lakebridge_dir / "tables.json", [{"name": "dbo.Orders"}])
    _write_csv(sqlglot_dir / "discovery_rollup.csv", [{"object_type": "table", "object_name": "(all)", "count": "1"}])
    _write_csv(lakebridge_dir / "lakebridge_rollup.csv", [{"object_type": "table", "object_name": "(all)", "count": "1"}])
    config = ComparisonConfig(sqlglot_output_dir=str(sqlglot_dir), lakebridge_output_dir=str(lakebridge_dir), output_dir=str(tmp_path / "out"))

    result = build_comparison(config)

    matching = [c for c in result.categories if c.category == "tables"]
    assert len(matching) == 1
    assert not any(c.category == "table" for c in result.categories)


def test_unsupported_objects_reads_real_json_not_narrow_recomputation(tmp_path):
    """Regression test for the drift this rewrite fixes: the old
    _unsupported_count_sqlglot only checked stored_procedures.json +
    packages.json embedded_sql, undercounting once views/functions/
    triggers/constraints started contributing to unsupported_objects.json
    too. This confirms the real file (covering every category) is read."""
    sqlglot_dir = tmp_path / "sqlglot"
    lakebridge_dir = tmp_path / "lakebridge"
    _write_json(sqlglot_dir / "unsupported_objects.json", [
        {"object_type": "stored_procedure", "name": "dbo.usp_A"},
        {"object_type": "view", "name": "dbo.vB"},
        {"object_type": "function", "name": "dbo.ufnC"},
    ])
    _write_json(lakebridge_dir / "unsupported_objects.json", [])
    config = ComparisonConfig(sqlglot_output_dir=str(sqlglot_dir), lakebridge_output_dir=str(lakebridge_dir), output_dir=str(tmp_path / "out"))

    result = build_comparison(config)

    unsupported = next(c for c in result.categories if c.category == "unsupported_objects")
    assert unsupported.sqlglot_count == 3  # not 1 (the old proc-only recomputation would have missed the view+function)
    assert unsupported.lakebridge_count == 0


def test_dependency_stats_read_verbatim_from_both_engines(tmp_path):
    sqlglot_dir = tmp_path / "sqlglot"
    lakebridge_dir = tmp_path / "lakebridge"
    _write_json(sqlglot_dir / "dependency_stats.json", {"by_relationship_type": {"reads": 5}})
    _write_json(lakebridge_dir / "dependency_stats.json", {"by_relationship_type": {"foreign_key": 3}})
    config = ComparisonConfig(sqlglot_output_dir=str(sqlglot_dir), lakebridge_output_dir=str(lakebridge_dir), output_dir=str(tmp_path / "out"))

    result = build_comparison(config)

    assert result.sqlglot_dependency_stats == {"by_relationship_type": {"reads": 5}}
    assert result.lakebridge_dependency_stats == {"by_relationship_type": {"foreign_key": 3}}


def test_warning_rollup_row_maps_to_warnings_category_with_its_note(tmp_path):
    """Regression guard: the rollup CSV's object_type is singular
    ("warning") but GENERATED_ARTIFACT_NOTES/the manifest field are named
    "warnings" (plural) -- confirms the alias map bridges the two so
    Requirement 7's documentation actually attaches."""
    sqlglot_dir = tmp_path / "sqlglot"
    lakebridge_dir = tmp_path / "lakebridge"
    _write_csv(sqlglot_dir / "discovery_rollup.csv", [{"object_type": "warning", "object_name": "(all)", "count": "23"}])
    _write_csv(lakebridge_dir / "lakebridge_rollup.csv", [{"object_type": "warning", "object_name": "(all)", "count": "0"}])
    config = ComparisonConfig(sqlglot_output_dir=str(sqlglot_dir), lakebridge_output_dir=str(lakebridge_dir), output_dir=str(tmp_path / "out"))

    result = build_comparison(config)

    by_category = {c.category: c for c in result.categories}
    assert "warning" not in by_category
    assert by_category["warnings"].sqlglot_count == 23
    assert "warnings" in result.category_notes


def test_category_notes_populated_only_for_categories_actually_present(tmp_path):
    sqlglot_dir = tmp_path / "sqlglot"
    lakebridge_dir = tmp_path / "lakebridge"
    _write_csv(sqlglot_dir / "discovery_rollup.csv", [{"object_type": "database_summary", "object_name": "(all)", "count": "1"}])
    _write_csv(lakebridge_dir / "lakebridge_rollup.csv", [{"object_type": "database_summary", "object_name": "(all)", "count": "1"}])
    config = ComparisonConfig(sqlglot_output_dir=str(sqlglot_dir), lakebridge_output_dir=str(lakebridge_dir), output_dir=str(tmp_path / "out"))

    result = build_comparison(config)

    assert "database_summary" in result.category_notes
    assert result.category_notes["database_summary"] == GENERATED_ARTIFACT_NOTES["database_summary"]
    assert "data_quality_summary" not in result.category_notes  # never present in this run's rollups
