"""Tests for ``chainstream_query_builder`` (offline, pure stdlib)."""
from __future__ import annotations

import argparse
from typing import Any, Dict

import pytest

from agentflow_pipeline.chainstream_query_builder import (
    DEFAULT_CHAINSTREAM_GRAPHQL_QUERY,
    DEFAULT_QUERY_SOURCE,
    build_probe_query,
    describe_probe_target,
    register_query_builder_args,
    resolve_probe_query,
    select_probe_target,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ns(**overrides: Any) -> argparse.Namespace:
    """Build an argparse.Namespace with the full set of probe flags."""
    base: Dict[str, Any] = {
        "chainstream_query": "",
        "chainstream_query_file": "",
        "chainstream_auto_build_query": False,
        "chainstream_probe_chain_group": "",
        "chainstream_probe_data_cube": "",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _fit_config(**fit: Any) -> Dict[str, Any]:
    return {"pre_build_analysis": {"chainstream_fit": fit}}


# ---------------------------------------------------------------------------
# build_probe_query
# ---------------------------------------------------------------------------

class TestBuildProbeQuery:
    def test_solana_dextrades_contains_solana_dextrades_block(self) -> None:
        q = build_probe_query("solana", "DEXTrades")
        assert "Solana {" in q
        assert "DEXTrades(" in q
        # Default limit threads through.
        assert "count: 1" in q

    def test_evm_dextrades_uses_evm_path(self) -> None:
        q = build_probe_query("evm", "DEXTrades")
        assert "EVM {" in q
        assert "DEXTrades(" in q
        # SmartContract instead of MintAddress.
        assert "SmartContract" in q
        assert "MintAddress" not in q

    def test_solana_tokens_returns_non_empty(self) -> None:
        q = build_probe_query("solana", "Tokens")
        assert q
        assert "Solana" in q
        assert "Tokens(" in q

    def test_evm_pairs_returns_non_empty(self) -> None:
        q = build_probe_query("evm", "Pairs")
        assert q
        assert "EVM {" in q
        assert "Pairs(" in q

    def test_solana_wallet_token_pnl_known(self) -> None:
        q = build_probe_query("solana", "WalletTokenPnL")
        assert "WalletTokenPnL(" in q
        assert "RealizedPnL" in q

    def test_unknown_combo_returns_introspection(self) -> None:
        q = build_probe_query("foo", "bar")
        assert "__schema" in q
        assert "queryType" in q
        # Introspection path should NOT carry a Solana / EVM cube.
        assert "DEXTrades" not in q

    def test_limit_param_threads_into_query_string(self) -> None:
        q = build_probe_query("solana", "DEXTrades", limit=7)
        assert "count: 7" in q
        assert "count: 1" not in q

    def test_limit_below_one_is_clamped_to_one(self) -> None:
        q = build_probe_query("solana", "DEXTrades", limit=0)
        assert "count: 1" in q

    def test_trading_chain_group_routes_to_solana(self) -> None:
        # 'trading' is a synthetic group; we prefer Solana for it.
        q = build_probe_query("trading", "DEXTrades")
        assert "Solana {" in q
        assert "EVM {" not in q

    def test_chain_group_is_case_insensitive(self) -> None:
        q = build_probe_query("SOLANA", "DEXTrades")
        assert "Solana {" in q

    def test_empty_chain_group_defaults_to_solana(self) -> None:
        q = build_probe_query("", "DEXTrades")
        assert "Solana {" in q


# ---------------------------------------------------------------------------
# select_probe_target
# ---------------------------------------------------------------------------

class TestSelectProbeTarget:
    def test_full_fit_returns_first_chain_and_cube(self) -> None:
        cfg = _fit_config(
            chain_groups=["evm", "trading"],
            data_cubes=["DEXTrades", "Tokens"],
            target_capability="graphql",
        )
        cg, cube, q = select_probe_target(cfg)
        assert cg == "evm"
        assert cube == "DEXTrades"
        assert "EVM {" in q

    def test_empty_chain_groups_uses_default_solana(self) -> None:
        cfg = _fit_config(chain_groups=[], data_cubes=[])
        cg, cube, q = select_probe_target(cfg)
        assert cg == "solana"
        assert cube == "DEXTrades"
        assert "Solana {" in q

    def test_missing_chainstream_fit_uses_defaults(self) -> None:
        cg, cube, q = select_probe_target({})
        assert (cg, cube) == ("solana", "DEXTrades")
        assert "Solana {" in q

    def test_solana_pairs_combination(self) -> None:
        cfg = _fit_config(chain_groups=["solana"], data_cubes=["Pairs"])
        cg, cube, q = select_probe_target(cfg)
        assert (cg, cube) == ("solana", "Pairs")
        assert "Pairs(" in q


# ---------------------------------------------------------------------------
# describe_probe_target
# ---------------------------------------------------------------------------

class TestDescribeProbeTarget:
    def test_solana_dextrades_label(self) -> None:
        assert describe_probe_target("solana", "DEXTrades") == "chainstream:solana.DEXTrades"

    def test_evm_pairs_label(self) -> None:
        assert describe_probe_target("evm", "Pairs") == "chainstream:evm.Pairs"

    def test_empty_args_fall_back_to_solana_dextrades(self) -> None:
        assert describe_probe_target("", "") == "chainstream:solana.DEXTrades"


# ---------------------------------------------------------------------------
# register_query_builder_args
# ---------------------------------------------------------------------------

class TestRegisterQueryBuilderArgs:
    def test_flags_register_with_expected_defaults(self) -> None:
        parser = argparse.ArgumentParser()
        # Inline + file flags are owned by run_pipeline; emulate them so the
        # auto-build flag sits next to them, mirroring the integration patch.
        parser.add_argument("--chainstream-query", default="")
        parser.add_argument("--chainstream-query-file", default="")
        register_query_builder_args(parser)
        ns = parser.parse_args([])
        assert ns.chainstream_auto_build_query is False
        assert ns.chainstream_probe_chain_group == ""
        assert ns.chainstream_probe_data_cube == ""

    def test_flags_can_be_set_from_cli(self) -> None:
        parser = argparse.ArgumentParser()
        register_query_builder_args(parser)
        ns = parser.parse_args(
            [
                "--chainstream-auto-build-query",
                "--chainstream-probe-chain-group", "evm",
                "--chainstream-probe-data-cube", "Transfers",
            ]
        )
        assert ns.chainstream_auto_build_query is True
        assert ns.chainstream_probe_chain_group == "evm"
        assert ns.chainstream_probe_data_cube == "Transfers"


# ---------------------------------------------------------------------------
# resolve_probe_query
# ---------------------------------------------------------------------------

class TestResolveProbeQuery:
    def test_inline_wins_over_everything(self, tmp_path) -> None:
        f = tmp_path / "q.graphql"
        f.write_text("{ Ignored }", encoding="utf-8")
        args = _ns(
            chainstream_query="{ Inline }",
            chainstream_query_file=str(f),
            chainstream_auto_build_query=True,
        )
        q, src = resolve_probe_query(args, _fit_config(chain_groups=["evm"]))
        assert q == "{ Inline }"
        assert src == "inline"

    def test_file_wins_over_auto_and_default(self, tmp_path) -> None:
        f = tmp_path / "probe.graphql"
        f.write_text("{ FromFile }", encoding="utf-8")
        args = _ns(
            chainstream_query_file=str(f),
            chainstream_auto_build_query=True,
        )
        q, src = resolve_probe_query(args, _fit_config(chain_groups=["evm"]))
        assert q == "{ FromFile }"
        # The query_source carries the resolved absolute path.
        assert src.endswith("probe.graphql")

    def test_file_missing_raises(self, tmp_path) -> None:
        args = _ns(chainstream_query_file=str(tmp_path / "does_not_exist.graphql"))
        with pytest.raises(FileNotFoundError):
            resolve_probe_query(args, {})

    def test_auto_uses_config_inferred_chain_and_cube(self) -> None:
        cfg = _fit_config(chain_groups=["evm"], data_cubes=["Transfers"])
        args = _ns(chainstream_auto_build_query=True)
        q, src = resolve_probe_query(args, cfg)
        assert "EVM {" in q
        assert "Transfers(" in q
        assert src == "chainstream:evm.Transfers"

    def test_explicit_args_override_config_inference(self) -> None:
        cfg = _fit_config(chain_groups=["solana"], data_cubes=["DEXTrades"])
        args = _ns(
            chainstream_auto_build_query=True,
            chainstream_probe_chain_group="evm",
            chainstream_probe_data_cube="Pairs",
        )
        q, src = resolve_probe_query(args, cfg)
        assert "EVM {" in q
        assert "Pairs(" in q
        assert src == "chainstream:evm.Pairs"

    def test_default_when_no_flags_and_no_auto(self) -> None:
        q, src = resolve_probe_query(_ns(), {})
        assert q == DEFAULT_CHAINSTREAM_GRAPHQL_QUERY
        assert src == DEFAULT_QUERY_SOURCE
        # And the default really is the Solana DEXTrades probe.
        assert "Solana {" in q
        assert "DEXTrades(" in q

    def test_auto_with_unknown_combo_falls_back_to_introspection(self) -> None:
        cfg = _fit_config(chain_groups=["unknown_chain"], data_cubes=["UnknownCube"])
        args = _ns(chainstream_auto_build_query=True)
        q, src = resolve_probe_query(args, cfg)
        assert "__schema" in q
        # Source still describes the intent so debugging stays clear.
        assert src == "chainstream:unknown_chain.UnknownCube"
