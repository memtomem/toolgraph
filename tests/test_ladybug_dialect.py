"""Unit contract for the intentionally narrow Ladybug dialect shim."""

from __future__ import annotations

import pytest

from toolgraph.graph.driver import BackendConfigurationError, _ladybug_query


def test_supported_expression_shapes_allow_aliases_and_whitespace():
    query = """
    WITH datetime ( ) AS now,
         type ( edge ) AS edge_type,
         labels ( source_node ) [ 0 ] AS source_label
    SET tool.read_only_hint = t.read_only_hint,
        tool.destructive_hint=t.destructive_hint
    """
    translated = _ladybug_query(query)
    assert "current_timestamp() AS now" in translated
    assert "label(edge) AS edge_type" in translated
    assert "label(source_node) AS source_label" in translated
    assert "tool.read_only_hint=CAST(t.read_only_hint AS BOOLEAN)" in translated
    assert "tool.destructive_hint=CAST(t.destructive_hint AS BOOLEAN)" in translated


def test_identifier_boundaries_do_not_rewrite_unrelated_names():
    query = "RETURN prototype(node) AS prototype, some_type(edge) AS some_type"
    assert _ladybug_query(query) == query


def test_unsupported_complex_neo4j_expression_fails_fast():
    with pytest.raises(BackendConfigurationError, match="unsupported Neo4j expression"):
        _ladybug_query("RETURN type(coalesce(left_edge, right_edge)) AS edge_type")
