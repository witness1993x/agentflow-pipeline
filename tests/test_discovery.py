"""Tests for discovery helpers in run_pipeline."""
from __future__ import annotations

import pytest

from agentflow_pipeline import cli as rp


# ---------------------------------------------------------------------------
# parse_discovery_sources
# ---------------------------------------------------------------------------

class TestParseDiscoverySources:
    def test_default_when_blank(self) -> None:
        assert rp.parse_discovery_sources("") == ["github"]
        assert rp.parse_discovery_sources("   ") == ["github"]

    def test_all_expands(self) -> None:
        result = rp.parse_discovery_sources("all")
        assert result == ["github", "jina", "x", "hackernews", "reddit"]

    def test_explicit_list_dedupes_preserve_order(self) -> None:
        result = rp.parse_discovery_sources("jina, github, JINA")
        # dedupe is case-insensitive
        assert result == ["jina", "github"]

    def test_unknown_source_raises(self) -> None:
        with pytest.raises(rp.PipelineError):
            rp.parse_discovery_sources("github,foo")


# ---------------------------------------------------------------------------
# dedupe_terms
# ---------------------------------------------------------------------------

class TestDedupeTerms:
    def test_strips_and_drops_blanks(self) -> None:
        assert rp.dedupe_terms(["  foo  ", "", "bar"]) == ["foo", "bar"]

    def test_case_insensitive_dedupe_keeps_first(self) -> None:
        assert rp.dedupe_terms(["Foo", "foo", "FOO"]) == ["Foo"]

    def test_empty_list(self) -> None:
        assert rp.dedupe_terms([]) == []


# ---------------------------------------------------------------------------
# discovery_query
# ---------------------------------------------------------------------------

class TestDiscoveryQuery:
    def test_override_wins(self) -> None:
        config = {
            "meta": {"hotspot_name": "ignored"},
            "gate_2_project_shape": {"project_shape": "indexer"},
            "gate_3_repo_routing": {"discovered_query": "saved one"},
        }
        assert rp.discovery_query(config, "explicit override") == "explicit override"

    def test_uses_saved_query_when_no_override(self) -> None:
        config = {
            "meta": {"hotspot_name": "demo"},
            "gate_2_project_shape": {"project_shape": "indexer"},
            "gate_3_repo_routing": {"discovered_query": "saved query string"},
        }
        assert rp.discovery_query(config, "") == "saved query string"

    def test_synthesises_from_meta_topic_and_shape(self) -> None:
        config = {
            "meta": {"hotspot_name": "solana hotspot"},
            "source_context": {"topic_lineage": ["solana", "dex", ""]},
            "gate_2_project_shape": {"project_shape": "agent_workflow"},
            "gate_3_repo_routing": {"discovered_query": ""},
        }
        result = rp.discovery_query(config, "")
        # First three terms come from meta + lineage; shape is normalised
        # ("agent_workflow" -> "agent workflow") and appended after dedupe.
        assert "solana hotspot" in result
        assert "agent workflow" in result
        # dedupe collapses repeated tokens (e.g. "solana" appearing in both
        # the hotspot name and the lineage when slugified) so we expect no
        # naive duplication of the lineage entry as a standalone term.
        assert result.count("agent workflow") == 1

    def test_undecided_shape_omitted(self) -> None:
        config = {
            "meta": {"hotspot_name": "thing"},
            "source_context": {"topic_lineage": []},
            "gate_2_project_shape": {"project_shape": "undecided"},
            "gate_3_repo_routing": {"discovered_query": ""},
        }
        result = rp.discovery_query(config, "")
        assert result == "thing"
