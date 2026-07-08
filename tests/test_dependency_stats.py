"""
Tests for autovista.dependency_stats.compute_dependency_stats -- pure
summary-statistics computation over an already-built dependency list, same
output shape as lakebridge_discovery.catalog_metadata._compute_stats.
"""
from __future__ import annotations

from autovista.dependency_stats import compute_dependency_stats
from autovista.schema import DependencyEntity


def _dep(source_object, source_type, target_object, target_type, relationship_type, discovery_method) -> DependencyEntity:
    return DependencyEntity(
        source_object=source_object, source_type=source_type, target_object=target_object,
        target_type=target_type, relationship_type=relationship_type, discovery_method=discovery_method,
    )


def test_empty_dependency_list_produces_zeroed_stats():
    stats = compute_dependency_stats([])
    assert stats == {
        "total_dependencies": 0,
        "unique_relationships": 0,
        "by_relationship_type": {},
        "by_type_pair": {},
        "by_discovery_method": {},
        "resolved": 0,
        "unresolved": 0,
    }


def test_counts_total_and_by_relationship_type():
    deps = [
        _dep("dbo.usp_A", "stored_procedure", "dbo.Orders", "table", "reads", "sqlglot"),
        _dep("dbo.usp_A", "stored_procedure", "dbo.Customers", "table", "reads", "sqlglot"),
        _dep("dbo.usp_A", "stored_procedure", "dbo.Orders", "table", "writes", "sqlglot"),
    ]
    stats = compute_dependency_stats(deps)
    assert stats["total_dependencies"] == 3
    assert stats["by_relationship_type"] == {"reads": 2, "writes": 1}


def test_by_type_pair_is_sorted_and_uses_arrow_format():
    deps = [
        _dep("dbo.V", "view", "dbo.T", "table", "reads", "sqlglot"),
        _dep("dbo.usp_A", "stored_procedure", "dbo.T", "table", "reads", "sqlglot"),
    ]
    stats = compute_dependency_stats(deps)
    assert stats["by_type_pair"] == {"stored_procedure->table": 1, "view->table": 1}
    assert list(stats["by_type_pair"].keys()) == sorted(stats["by_type_pair"].keys())


def test_by_discovery_method_counts_direct_metadata_and_sqlglot_separately():
    deps = [
        _dep("dbo.A", "table", "dbo.B", "table", "foreign_key", "direct_metadata"),
        _dep("dbo.usp_A", "stored_procedure", "dbo.B", "table", "reads", "sqlglot"),
        _dep("dbo.usp_C", "stored_procedure", "dbo.D", "table", "reads", "sqlglot"),
    ]
    stats = compute_dependency_stats(deps)
    assert stats["by_discovery_method"] == {"direct_metadata": 1, "sqlglot": 2}


def test_unresolved_discovery_method_is_excluded_from_resolved_count():
    deps = [
        _dep("dbo.usp_A", "stored_procedure", "dbo.T", "table", "reads", "sqlglot"),
        _dep("dbo.usp_B", "stored_procedure", "unknown", "unknown", "reads", "unresolved"),
    ]
    stats = compute_dependency_stats(deps)
    assert stats["resolved"] == 1
    assert stats["unresolved"] == 1


def test_unique_relationships_deduplicates_identical_source_target_relationship_triples():
    deps = [
        _dep("dbo.usp_A", "stored_procedure", "dbo.T", "table", "reads", "sqlglot"),
        _dep("dbo.usp_A", "stored_procedure", "dbo.T", "table", "reads", "direct_metadata"),
    ]
    stats = compute_dependency_stats(deps)
    assert stats["total_dependencies"] == 2
    assert stats["unique_relationships"] == 1
