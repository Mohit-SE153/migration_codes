"""
Offline (no live DB) tests for tools/dependency_validator/ -- normalization
and the 4-category classification logic, against small synthetic
ground-truth rows and a synthetic SQLGlot-side dependency list.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from tools.dependency_validator.classify import (
    KNOWN_UNSUPPORTED,
    MATCHED,
    MISSING,
    OUT_OF_SCOPE,
    build_sqlglot_key_sets,
    classify_expression_dependency,
    classify_foreign_key,
    classify_synonym,
    classify_trigger_fires_on,
)
from tools.dependency_validator.normalize import normalize_identifier, strict_and_loose_keys
from tools.dependency_validator.report import build_coverage_report, category_label, write_reports
from tools.dependency_validator.sql_server_catalog import ExpressionDependencyRow, ForeignKeyRow, SynonymRow, TriggerFiresOnRow

HOME_DB = "AdventureWorks2022"


def _row(**overrides) -> ExpressionDependencyRow:
    base = dict(
        referencing_schema="dbo", referencing_name="usp_a", referencing_type="SQL_STORED_PROCEDURE",
        referencing_minor_id=0,
        referenced_server_name=None, referenced_database_name=None,
        referenced_schema_name="dbo", referenced_entity_name="Orders",
        referenced_class_desc="OBJECT_OR_COLUMN", referenced_minor_id=0,
        is_schema_bound_reference=False, is_ambiguous=False,
        referenced_type_desc="USER_TABLE",
    )
    base.update(overrides)
    return ExpressionDependencyRow(**base)


# --- normalize.py ---

def test_normalize_identifier_strips_brackets_quotes_case_per_segment():
    assert normalize_identifier("[dbo].[Person]") == "dbo.person"
    assert normalize_identifier(' "DBO" . "Person" ') == "dbo.person"
    assert normalize_identifier("dbo.Person") == "dbo.person"


def test_strict_and_loose_keys_drops_matching_home_database_prefix():
    strict, loose = strict_and_loose_keys("AdventureWorks2022.dbo.Person", HOME_DB)
    assert strict == "adventureworks2022.dbo.person"
    assert loose == "dbo.person"


def test_strict_and_loose_keys_unaffected_for_two_part_name():
    strict, loose = strict_and_loose_keys("dbo.Person", HOME_DB)
    assert strict == loose == "dbo.person"


def test_strict_and_loose_keys_unaffected_for_four_part_linked_server_name():
    strict, loose = strict_and_loose_keys("Server1.OtherDB.dbo.SomeTable", HOME_DB)
    assert strict == loose == "server1.otherdb.dbo.sometable"


# --- classify.py: expression dependencies ---

def test_matched_object_or_column_dependency():
    sqlglot_deps = [{"source_object": "dbo.usp_a", "source_type": "stored_procedure",
                      "target_object": "dbo.Orders", "target_type": "table", "relationship_type": "reads"}]
    strict, loose = build_sqlglot_key_sets(sqlglot_deps, HOME_DB)
    dep = classify_expression_dependency(_row(), {}, set(), strict, loose, HOME_DB)
    assert dep.category == MATCHED
    assert not dep.representation_difference


def test_representation_difference_when_only_loose_match():
    sqlglot_deps = [{"source_object": "dbo.usp_a", "source_type": "stored_procedure",
                      "target_object": "dbo.Orders", "target_type": "table", "relationship_type": "reads"}]
    strict, loose = build_sqlglot_key_sets(sqlglot_deps, HOME_DB)
    row = _row(referenced_database_name="AdventureWorks2022")  # redundant home-DB qualification
    dep = classify_expression_dependency(row, {}, set(), strict, loose, HOME_DB)
    assert dep.category == MATCHED
    assert dep.representation_difference is True


def test_missing_when_no_match_at_all():
    strict, loose = build_sqlglot_key_sets([], HOME_DB)
    dep = classify_expression_dependency(_row(), {}, set(), strict, loose, HOME_DB)
    assert dep.category == MISSING


def test_column_level_reference_is_out_of_scope():
    strict, loose = build_sqlglot_key_sets([], HOME_DB)
    row = _row(referenced_minor_id=3)
    dep = classify_expression_dependency(row, {}, set(), strict, loose, HOME_DB)
    assert dep.category == OUT_OF_SCOPE
    assert "column-level" in dep.reason


def test_internal_metadata_class_is_out_of_scope():
    strict, loose = build_sqlglot_key_sets([], HOME_DB)
    row = _row(referenced_class_desc="DATABASE", referenced_type_desc=None)
    dep = classify_expression_dependency(row, {}, set(), strict, loose, HOME_DB)
    assert dep.category == OUT_OF_SCOPE
    assert "internal metadata class" in dep.reason


def test_ambiguous_reference_is_out_of_scope():
    strict, loose = build_sqlglot_key_sets([], HOME_DB)
    row = _row(is_ambiguous=True)
    dep = classify_expression_dependency(row, {}, set(), strict, loose, HOME_DB)
    assert dep.category == OUT_OF_SCOPE
    assert "ambiguous" in dep.reason


def test_pseudo_table_is_out_of_scope():
    strict, loose = build_sqlglot_key_sets([], HOME_DB)
    row = _row(referencing_type="SQL_TRIGGER", referenced_entity_name="inserted", referenced_database_name=None, referenced_schema_name=None, referenced_type_desc=None)
    dep = classify_expression_dependency(row, {}, set(), strict, loose, HOME_DB)
    assert dep.category == OUT_OF_SCOPE
    assert "inserted/deleted" in dep.reason


def test_type_class_produces_user_defined_type_edge():
    sqlglot_deps = [{"source_object": "dbo.usp_a", "source_type": "stored_procedure",
                      "target_object": "dbo.Flag", "target_type": "user_defined_type", "relationship_type": "uses_type"}]
    strict, loose = build_sqlglot_key_sets(sqlglot_deps, HOME_DB)
    row = _row(referenced_class_desc="TYPE", referenced_entity_name="Flag", referenced_type_desc=None)
    dep = classify_expression_dependency(row, {}, set(), strict, loose, HOME_DB)
    assert dep.target_type == "user_defined_type"
    assert dep.category == MATCHED


def test_xml_namespace_class_produces_xml_schema_collection_edge():
    strict, loose = build_sqlglot_key_sets([], HOME_DB)
    row = _row(referenced_class_desc="XML_NAMESPACE", referenced_entity_name="MySchema", referenced_type_desc=None)
    dep = classify_expression_dependency(row, {}, set(), strict, loose, HOME_DB)
    assert dep.target_type == "xml_schema_collection"
    assert dep.category == MISSING  # not present in the (empty) sqlglot set


def test_dynamic_sql_gates_to_known_unsupported():
    strict, loose = build_sqlglot_key_sets([], HOME_DB)
    dynamic_sql_objects = {("stored_procedure", "dbo", "usp_a")}
    dep = classify_expression_dependency(_row(), {}, dynamic_sql_objects, strict, loose, HOME_DB)
    assert dep.category == KNOWN_UNSUPPORTED


def test_dynamic_sql_gating_does_not_override_a_real_match():
    """If the object is flagged dynamic-SQL but this specific ground-truth
    edge still matches (e.g. a static portion of the body was resolved),
    it must be reported as MATCHED, not silently swallowed into
    KNOWN_UNSUPPORTED."""
    sqlglot_deps = [{"source_object": "dbo.usp_a", "source_type": "stored_procedure",
                      "target_object": "dbo.Orders", "target_type": "table", "relationship_type": "reads"}]
    strict, loose = build_sqlglot_key_sets(sqlglot_deps, HOME_DB)
    dynamic_sql_objects = {("stored_procedure", "dbo", "usp_a")}
    dep = classify_expression_dependency(_row(), {}, dynamic_sql_objects, strict, loose, HOME_DB)
    assert dep.category == MATCHED


def test_constraint_source_object_resolved_via_constraint_full_id_map():
    constraint_full_id = {("dbo", "ck_orders_total"): "dbo.Orders.CK_Orders_Total"}
    strict, loose = build_sqlglot_key_sets([], HOME_DB)
    row = _row(referencing_schema="dbo", referencing_name="CK_Orders_Total", referencing_type="CHECK_CONSTRAINT")
    dep = classify_expression_dependency(row, constraint_full_id, set(), strict, loose, HOME_DB)
    assert dep.source_object == "dbo.Orders.CK_Orders_Total"


def test_unrecognized_referencing_type_is_out_of_scope():
    strict, loose = build_sqlglot_key_sets([], HOME_DB)
    row = _row(referencing_type="SERVICE_QUEUE")
    dep = classify_expression_dependency(row, {}, set(), strict, loose, HOME_DB)
    assert dep.category == OUT_OF_SCOPE
    assert "not modeled" in dep.reason


# --- classify.py: foreign keys / synonyms / trigger fires_on ---

def test_foreign_key_matched():
    sqlglot_deps = [{"source_object": "Sales.SalesOrderDetail", "source_type": "table",
                      "target_object": "Sales.SalesOrderHeader", "target_type": "table", "relationship_type": "foreign_key"}]
    strict, loose = build_sqlglot_key_sets(sqlglot_deps, HOME_DB)
    row = ForeignKeyRow(parent_schema="Sales", parent_table="SalesOrderDetail", ref_schema="Sales", ref_table="SalesOrderHeader")
    dep = classify_foreign_key(row, strict, loose, HOME_DB)
    assert dep.category == MATCHED


def test_synonym_missing_when_not_present():
    strict, loose = build_sqlglot_key_sets([], HOME_DB)
    row = SynonymRow(schema="dbo", name="CustomerAlias", base_object_name="dbo.Customers", base_object_type_desc="USER_TABLE")
    dep = classify_synonym(row, strict, loose, HOME_DB)
    assert dep.category == MISSING
    assert dep.target_type == "table"


def test_trigger_fires_on_matches_bare_table_name():
    sqlglot_deps = [{"source_object": "Sales.trg_Audit", "source_type": "trigger",
                      "target_object": "SalesOrderDetail", "target_type": "table", "relationship_type": "fires_on"}]
    strict, loose = build_sqlglot_key_sets(sqlglot_deps, HOME_DB)
    row = TriggerFiresOnRow(schema="Sales", name="trg_Audit", table_name="SalesOrderDetail")
    dep = classify_trigger_fires_on(row, strict, loose, HOME_DB)
    assert dep.category == MATCHED


# --- report.py ---

def test_category_label_distinguishes_check_and_default_constraint():
    dep = classify_expression_dependency(_row(referencing_type="CHECK_CONSTRAINT"), {}, set(), set(), set(), HOME_DB)
    assert category_label(dep, "CHECK_CONSTRAINT") == "Check Constraint -> Table"
    dep2 = classify_expression_dependency(_row(referencing_type="DEFAULT_CONSTRAINT"), {}, set(), set(), set(), HOME_DB)
    assert category_label(dep2, "DEFAULT_CONSTRAINT") == "Default Constraint -> Table"


def test_category_label_special_cases_foreign_keys_and_synonyms():
    fk_dep = classify_foreign_key(
        ForeignKeyRow(parent_schema="dbo", parent_table="A", ref_schema="dbo", ref_table="B"), set(), set(), HOME_DB,
    )
    assert category_label(fk_dep) == "Foreign Keys"
    syn_dep = classify_synonym(
        SynonymRow(schema="dbo", name="Alias", base_object_name="dbo.T", base_object_type_desc="USER_TABLE"), set(), set(), HOME_DB,
    )
    assert category_label(syn_dep) == "Synonyms"


def test_coverage_by_category_sums_sqlglot_count_across_relationship_types():
    """A single category label ("Trigger -> Table") can span more than one
    relationship_type (reads + fires_on) -- the SQLGlot-side count must be
    the sum across both shapes, not whichever was processed last."""
    sqlglot_deps = [
        {"source_object": "Sales.trg_a", "source_type": "trigger", "target_object": "Sales.Orders",
         "target_type": "table", "relationship_type": "reads"},
        {"source_object": "Sales.trg_a", "source_type": "trigger", "target_object": "Sales.Orders",
         "target_type": "table", "relationship_type": "fires_on"},
        {"source_object": "Sales.trg_b", "source_type": "trigger", "target_object": "Sales.Customers",
         "target_type": "table", "relationship_type": "fires_on"},
    ]
    strict, loose = build_sqlglot_key_sets(sqlglot_deps, HOME_DB)
    from tools.dependency_validator.classify import _match_or_missing
    reads = _match_or_missing("Sales.trg_a", "trigger", "Sales.Orders", "table", "reads", strict, loose, HOME_DB)
    fires_on = _match_or_missing("Sales.trg_a", "trigger", "Sales.Orders", "table", "fires_on", strict, loose, HOME_DB)
    report = build_coverage_report([(reads, None), (fires_on, None)], sqlglot_deps)
    assert report["by_category"]["Trigger -> Table"]["sqlglot_count"] == 3


def test_coverage_report_excludes_known_unsupported_from_coverage_denominator():
    matched = classify_expression_dependency(
        _row(), {}, set(),
        *build_sqlglot_key_sets(
            [{"source_object": "dbo.usp_a", "source_type": "stored_procedure",
              "target_object": "dbo.Orders", "target_type": "table", "relationship_type": "reads"}], HOME_DB,
        ), HOME_DB,
    )
    unsupported = classify_expression_dependency(
        _row(referencing_name="usp_b", referenced_entity_name="OtherTable"), {},
        {("stored_procedure", "dbo", "usp_b")}, set(), set(), HOME_DB,
    )
    report = build_coverage_report([(matched, "SQL_STORED_PROCEDURE"), (unsupported, "SQL_STORED_PROCEDURE")], [])
    assert report["migration_relevant"]["matched"] == 1
    assert report["migration_relevant"]["known_unsupported"] == 1
    assert report["migration_relevant"]["coverage_pct"] == 100.0  # 1 matched / (1 matched + 0 missing), unsupported excluded


# --- report.py: write_reports actually persists files to disk ---
#
# These two tests manage their own scratch directory under the repo root
# rather than pytest's tmp_path fixture or Python's tempfile/%TEMP%.
# Both were tried first and found unreliable on this project's own Windows
# dev machine: tmp_path's internal lock-file creation intermittently fails
# ("OSError: lock path got renamed after successful creation"), and even a
# bare Path.write_text() immediately followed by Path.exists() in the same
# process disagreed under %TEMP% specifically -- consistent with an
# antivirus/EDR product intercepting file writes there. D:\autovista itself
# has been repeatedly, reliably confirmed to persist files correctly
# (verified across multiple live tools/validate_sqlglot_dependencies.py
# runs), so the scratch directory lives there instead.
_SCRATCH_ROOT = Path(__file__).resolve().parent.parent / "_test_scratch"


def _fresh_scratch_dir() -> Path:
    try:
        _SCRATCH_ROOT.mkdir(exist_ok=True)
    except FileExistsError:
        pass  # see report.py's _ensure_output_dir -- same environment quirk
    path = Path(tempfile.mkdtemp(prefix="validator_test_", dir=_SCRATCH_ROOT))
    return path


def _scanned_sizes(directory: Path) -> dict[str, int]:
    """os.scandir()-based existence/size check, not Path.exists()/
    Path.stat() -- this project's dev machine has a filesystem minifilter
    (security software hooking attribute-query I/O) that makes
    exists()/stat() report a genuinely-present, correctly-sized file as
    absent, while directory enumeration is unaffected. See report.py's
    write_reports() for the same reasoning applied to the production code."""
    with os.scandir(directory) as it:
        return {entry.name: entry.stat().st_size for entry in it}


def test_write_reports_creates_directory_and_all_five_report_files():
    """Regression test for a real failure mode: write_reports() appeared to
    succeed (no exception, console summary printed) but no output_validation
    folder was ever found anywhere in the repo. Root cause was a bare
    relative output path evaluated against whatever cwd the process
    happened to have. This asserts every expected file actually exists,
    with real content, at the exact path passed in -- not just that
    write_reports() returns without raising."""
    matched = classify_expression_dependency(
        _row(), {}, set(),
        *build_sqlglot_key_sets(
            [{"source_object": "dbo.usp_a", "source_type": "stored_procedure",
              "target_object": "dbo.Orders", "target_type": "table", "relationship_type": "reads"}], HOME_DB,
        ), HOME_DB,
    )
    missing = classify_expression_dependency(
        _row(referencing_name="usp_b", referenced_entity_name="OtherTable"), {}, set(), set(), set(), HOME_DB,
    )
    classified = [(matched, "SQL_STORED_PROCEDURE"), (missing, "SQL_STORED_PROCEDURE")]

    temp_root = _fresh_scratch_dir()
    try:
        output_dir = temp_root / "output_validation"
        report = write_reports(classified, [], str(output_dir))

        expected_files = [
            "coverage_summary.json",
            "missing_dependencies.csv",
            "out_of_scope.csv",
            "representation_differences.csv",
            "coverage_by_category.csv",
        ]
        sizes = _scanned_sizes(output_dir)
        for filename in expected_files:
            assert filename in sizes, f"{filename} was not written"
            assert sizes[filename] > 0, f"{filename} was written but is empty"

        assert report["_report_dir"] == str(output_dir.resolve())
        assert len(report["_report_files"]) == len(expected_files)
        for path_str in report["_report_files"]:
            assert Path(path_str).name in sizes
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_write_reports_resolves_relative_output_dir_against_cwd_not_silently():
    """A relative output_dir must resolve against the actual cwd at call
    time (so it's always an absolute, predictable path in the returned
    report), rather than silently landing somewhere unverified."""
    temp_root = _fresh_scratch_dir()
    original_cwd = Path.cwd()
    try:
        os.chdir(temp_root)
        matched = classify_expression_dependency(
            _row(), {}, set(),
            *build_sqlglot_key_sets(
                [{"source_object": "dbo.usp_a", "source_type": "stored_procedure",
                  "target_object": "dbo.Orders", "target_type": "table", "relationship_type": "reads"}], HOME_DB,
            ), HOME_DB,
        )
        report = write_reports([(matched, "SQL_STORED_PROCEDURE")], [], "relative_output_validation")
        resolved = temp_root / "relative_output_validation"
        assert report["_report_dir"] == str(resolved.resolve())
        assert _scanned_sizes(resolved).get("coverage_summary.json", 0) > 0
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(temp_root, ignore_errors=True)
