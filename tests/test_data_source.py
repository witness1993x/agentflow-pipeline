"""Tests for the pluggable :mod:`agentflow_pipeline.data_source` protocol.

The module abstracts away ChainStream as a first-class assumption so the
pipeline can plug alternative GraphQL data sources (Bitquery, The Graph, …)
without touching ``cli.py``.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from typing import Any, Dict

import pytest

from agentflow_pipeline import cli as rp
from agentflow_pipeline import data_source as ds


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _candidate(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "name": "kafka-streams-defi",
        "description": "Kafka streams pipeline for DeFi analytics on Solana",
        "fit_reason": "",
        "homepage": "",
        "language": "python",
        "license_note": "MIT",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _restore_data_source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the env var so cross-test leakage cannot pin the default plugin."""
    monkeypatch.delenv("AGENTFLOW_DATA_SOURCE", raising=False)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_chainstream_satisfies_data_source_plugin(self) -> None:
        plugin = ds.ChainStreamDataSource()
        assert isinstance(plugin, ds.DataSourcePlugin)
        assert plugin.name == "chainstream"
        assert plugin.gate_field == "chainstream_fit"

    def test_bitquery_satisfies_data_source_plugin(self) -> None:
        plugin = ds.BitqueryDataSource()
        assert isinstance(plugin, ds.DataSourcePlugin)
        assert plugin.name == "bitquery"
        # Demonstrates the gate field is plugin-configurable.
        assert plugin.gate_field == "bitquery_fit"

    def test_required_callable_attributes_present(self) -> None:
        plugin = ds.BitqueryDataSource()
        # Every method declared on the protocol must be a real callable.
        for method in (
            "assess_fit",
            "infer_targets",
            "keyword_dict",
            "default_probe_query",
            "build_probe_query",
            "select_probe_target",
            "post_graphql_probe",
        ):
            assert callable(getattr(plugin, method)), method


# ---------------------------------------------------------------------------
# ChainStreamDataSource.assess_fit cross-check vs cli.assess_chainstream_fit
# ---------------------------------------------------------------------------


class TestChainStreamAssessFitParity:
    """The plugin must reproduce ``cli.assess_chainstream_fit`` byte-for-byte.

    ``cli.assess_chainstream_fit`` is now a thin wrapper around the plugin so
    these two should return equal tuples for any candidate. We exercise a
    few representative shapes to lock the parity in.
    """

    @pytest.mark.parametrize(
        "candidate",
        [
            _candidate(),
            _candidate(name="graphql-explorer", description="GraphQL gateway for on-chain data", language="typescript"),
            _candidate(name="kafka-streams-defi", description="Kafka streams pipeline for DeFi events", language="java"),
            _candidate(name="archived-thing", description="graphql kafka solana ethereum dex analytics", is_archived=True),
            _candidate(name="empty-candidate", description="", fit_reason="", language=""),
            _candidate(name="solana-wallet-pnl", description="Wallet PnL dashboard for Solana DEX trades", language="rust"),
        ],
    )
    def test_plugin_matches_legacy_wrapper(self, candidate: Dict[str, Any]) -> None:
        plugin_result = ds.ChainStreamDataSource().assess_fit(candidate)
        legacy_result = rp.assess_chainstream_fit(candidate)
        assert plugin_result == legacy_result


class TestChainStreamInferTargetsParity:
    @pytest.mark.parametrize(
        "candidate",
        [
            _candidate(),
            _candidate(description="solana raydium swap analytics dashboard"),
            _candidate(description="ethereum evm transfers", recommended_chainstream_access="kafka"),
            _candidate(description="ohlc price chart", recommended_chainstream_access="websocket"),
        ],
    )
    def test_plugin_matches_legacy_wrapper(self, candidate: Dict[str, Any]) -> None:
        assert ds.ChainStreamDataSource().infer_targets(candidate) == rp.infer_chainstream_targets(candidate)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_built_ins_pre_registered(self) -> None:
        names = ds.registered_data_sources()
        assert "chainstream" in names
        assert "bitquery" in names

    def test_get_data_source_returns_registered_plugin(self) -> None:
        plugin = ds.get_data_source("chainstream")
        assert plugin.name == "chainstream"
        assert isinstance(plugin, ds.ChainStreamDataSource)

    def test_get_data_source_unknown_raises(self) -> None:
        with pytest.raises(ds.DataSourceError, match="Unknown data source"):
            ds.get_data_source("does_not_exist")

    def test_get_data_source_empty_name_raises(self) -> None:
        with pytest.raises(ds.DataSourceError, match="Empty data-source name"):
            ds.get_data_source("")

    def test_register_then_lookup_roundtrip(self) -> None:
        class _Tmp:
            name = "_tmp_roundtrip"
            gate_field = "tmp_fit"

            def assess_fit(self, candidate: dict) -> tuple[int, str, str]:
                return 0, "", "graphql"

            def infer_targets(self, candidate: dict) -> dict:
                return {}

            def keyword_dict(self) -> dict[str, list[str]]:
                return {}

            def default_probe_query(self) -> str:
                return ""

            def build_probe_query(
                self, chain_group: str, data_cube: str, *, limit: int = 1
            ) -> str:
                return ""

            def select_probe_target(self, config: dict) -> tuple[str, str, str]:
                return "", "", ""

            def post_graphql_probe(
                self, endpoint: str, api_key: str, query: str
            ) -> dict:
                return {}

        plugin = _Tmp()
        try:
            ds.register_data_source(plugin)
            assert ds.get_data_source("_tmp_roundtrip") is plugin
        finally:
            # Hand-clean the registry — there is no public unregister API and
            # we want the rest of the suite to see only built-ins.
            ds._REGISTRY.pop("_tmp_roundtrip", None)

    def test_register_rejects_non_protocol(self) -> None:
        class _Bad:
            name = "broken"
            # missing all the required methods

        with pytest.raises(ds.DataSourceError):
            ds.register_data_source(_Bad())  # type: ignore[arg-type]

    def test_register_rejects_empty_name(self) -> None:
        plugin = ds.ChainStreamDataSource()
        plugin.name = ""  # type: ignore[misc]
        try:
            with pytest.raises(ds.DataSourceError, match="non-empty string"):
                ds.register_data_source(plugin)
        finally:
            plugin.name = "chainstream"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# default_data_source — env-var driven selection
# ---------------------------------------------------------------------------


class TestDefaultDataSource:
    def test_default_without_env_var_is_chainstream(self) -> None:
        assert ds.default_data_source().name == "chainstream"

    def test_env_var_switches_default_plugin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTFLOW_DATA_SOURCE", "bitquery")
        assert ds.default_data_source().name == "bitquery"

    def test_env_var_unknown_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTFLOW_DATA_SOURCE", "thegraph_unknown")
        with pytest.raises(ds.DataSourceError):
            ds.default_data_source()


# ---------------------------------------------------------------------------
# CLI integration: --data-source flag + resolve_data_source
# ---------------------------------------------------------------------------


def _ns(**overrides: Any) -> argparse.Namespace:
    base: Dict[str, Any] = {"data_source": ""}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestCliResolveDataSource:
    def test_unknown_data_source_flag_raises_pipeline_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENTFLOW_DATA_SOURCE", raising=False)
        args = _ns(data_source="thegraph_unknown")
        with pytest.raises(rp.PipelineError, match="Unknown data source"):
            rp.resolve_data_source(args)

    def test_explicit_flag_overrides_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTFLOW_DATA_SOURCE", "chainstream")
        args = _ns(data_source="bitquery")
        plugin = rp.resolve_data_source(args)
        assert plugin.name == "bitquery"

    def test_env_var_used_when_flag_blank(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTFLOW_DATA_SOURCE", "bitquery")
        plugin = rp.resolve_data_source(_ns())
        assert plugin.name == "bitquery"

    def test_default_when_neither_flag_nor_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENTFLOW_DATA_SOURCE", raising=False)
        plugin = rp.resolve_data_source(_ns())
        assert plugin.name == "chainstream"


# ---------------------------------------------------------------------------
# pre_build_analysis.<gate_field> respects plugin selection
# ---------------------------------------------------------------------------


def _discover_config() -> Dict[str, Any]:
    """Minimal config shape that ``update_pre_build_analysis`` walks."""
    return {
        "meta": {"hotspot_name": "demo", "hotspot_id": "HSP-T", "date": "2026-05-01"},
        "gate_2_project_shape": {"project_shape": "indexer"},
        "gate_3_repo_routing": {
            "repo_strategy": "fork_existing",
            "candidate_repos": [],
        },
        "pre_build_analysis": {},
    }


class TestGateFieldPerPlugin:
    def test_default_writes_chainstream_fit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default plugin must keep writing ``chainstream_fit`` for backwards compatibility."""
        monkeypatch.delenv("AGENTFLOW_DATA_SOURCE", raising=False)
        config = _discover_config()
        candidate = {
            "name": "demo/repo",
            "description": "Solana DEX analytics dashboard",
            "language": "python",
            "fit_reason": "",
            "license_note": "MIT",
        }
        rp.enrich_candidate(config, candidate)
        rp.update_pre_build_analysis(config, [candidate])
        assert "chainstream_fit" in config["pre_build_analysis"]
        assert "bitquery_fit" not in config["pre_build_analysis"]
        # The block must have all the expected keys the gate template reads.
        cs = config["pre_build_analysis"]["chainstream_fit"]
        for key in (
            "score",
            "verdict",
            "target_capability",
            "best_access_method",
            "chain_groups",
            "data_cubes",
            "query_intent",
            "fit_reason",
        ):
            assert key in cs

    def test_bitquery_plugin_writes_bitquery_fit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTFLOW_DATA_SOURCE", "bitquery")
        config = _discover_config()
        candidate = {
            "name": "demo/repo",
            "description": "Solana DEX analytics dashboard via Bitquery",
            "language": "python",
            "fit_reason": "",
            "license_note": "MIT",
        }
        rp.enrich_candidate(config, candidate)
        rp.update_pre_build_analysis(config, [candidate])
        assert "bitquery_fit" in config["pre_build_analysis"]
        # Must NOT also write chainstream_fit when bitquery is the active plugin.
        assert "chainstream_fit" not in config["pre_build_analysis"]
        # Doc refs come from the Bitquery plugin, not ChainStream.
        refs = config["pre_build_analysis"]["bitquery_fit"]["api_doc_refs"]
        assert any("bitquery" in r.lower() for r in refs)

    def test_data_probe_writeback_uses_plugin_gate_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``update_gate_after_data_probe`` must write under the active plugin's gate."""
        monkeypatch.setenv("AGENTFLOW_DATA_SOURCE", "bitquery")
        config = {
            "decision": {"final_status": "draft"},
            "execution_state": {},
            "pre_build_analysis": {"bitquery_fit": {"score": 0}},
            "gate_4_buildability": {"verdict": "fail"},
            "gate_5_publish_decision": {"verdict": "fail"},
            "execution_state": {},
        }
        result = {
            "status": "passed",
            "endpoint": "https://graphql.bitquery.io/",
            "query_source": "default_solana_dextrades",
            "summary": "ok",
            "response_keys": ["Solana"],
            "credits": {},
        }
        rp.update_gate_after_data_probe(config, result)
        assert config["pre_build_analysis"]["bitquery_fit"]["verdict"] == "pass"
        assert "graphql_probe" in config["pre_build_analysis"]["bitquery_fit"]
        # No chainstream_fit field gets created on the side.
        assert "chainstream_fit" not in config["pre_build_analysis"]


# ---------------------------------------------------------------------------
# Probe query / select_probe_target plumbing per plugin
# ---------------------------------------------------------------------------


class TestProbeBehaviour:
    def test_chainstream_default_query_is_solana_dextrades(self) -> None:
        plugin = ds.ChainStreamDataSource()
        q = plugin.default_probe_query()
        assert "Solana" in q
        assert "DEXTrades" in q

    def test_bitquery_select_probe_target_reads_its_own_gate_field(self) -> None:
        plugin = ds.BitqueryDataSource()
        config = {
            "pre_build_analysis": {
                "bitquery_fit": {
                    "chain_groups": ["evm"],
                    "data_cubes": ["DEXTrades"],
                }
            }
        }
        chain_group, data_cube, query = plugin.select_probe_target(config)
        assert chain_group == "evm"
        assert data_cube == "DEXTrades"
        assert "EVM" in query and "DEXTrades" in query

    def test_bitquery_keyword_dict_extends_chainstream_domain(self) -> None:
        chain_kw = ds.ChainStreamDataSource().keyword_dict()
        bq_kw = ds.BitqueryDataSource().keyword_dict()
        # Bitquery covers more L2s — strict superset.
        assert set(chain_kw["domain"]).issubset(set(bq_kw["domain"]))
        assert "arbitrum" in bq_kw["domain"]
        assert "arbitrum" not in chain_kw["domain"]
