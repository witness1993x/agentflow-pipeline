"""ChainStream GraphQL query builder for the data-probe gate.

This module replaces the hard-coded Solana DEXTrades probe in ``run_pipeline.py``
with a *case-aware* dynamic builder. Each case's
``pre_build_analysis.chainstream_fit`` block already carries:

    chain_groups: list[str]      # e.g. ["solana"], ["evm", "trading"]
    data_cubes:   list[str]      # e.g. ["DEXTrades"], ["WalletTokenPnL", "Tokens"]
    target_capability: str       # "graphql" | "kafka" | "websocket"
    query_intent: str            # free-form description

We use those hints to build a *minimal* probe (limit=1) targeted at the
correct chain + cube. Combinations not in the built-in template map fall
back to a credit-free ``__schema`` introspection query.

Pure stdlib. No ChainStream credits are spent at build time.

------------------------------------------------------------------------
Integration patch for ``run_pipeline.py`` (DO NOT APPLY HERE)
------------------------------------------------------------------------

1.  Add the import near the other top-of-file imports::

        from chainstream_query_builder import (
            register_query_builder_args,
            resolve_probe_query,
        )

2.  In ``parse_args`` (right after the existing ``--chainstream-query-file``
    argument is registered) call::

        register_query_builder_args(parser)

3.  ``chainstream_query_from_args`` currently has the signature::

        def chainstream_query_from_args(args: argparse.Namespace) -> tuple[str, str]:

    **It must be widened to accept ``config``** so the auto-builder can read
    ``pre_build_analysis.chainstream_fit``::

        def chainstream_query_from_args(
            args: argparse.Namespace,
            config: dict | None = None,
        ) -> tuple[str, str]:
            return resolve_probe_query(args, config or {})

4.  Update the single call site in ``run_chainstream_data_probe``::

        - query, query_source = chainstream_query_from_args(args)
        + query, query_source = chainstream_query_from_args(args, config)

    (``run_chainstream_data_probe`` is called from the orchestrator that
    already has ``config`` in scope; thread it through.)

5.  ``query_source`` no longer hard-codes ``"default_solana_dextrades"`` for
    auto-built queries; it becomes ``"chainstream:<chain_group>.<data_cube>"``
    via :func:`describe_probe_target`. The original default string is still
    returned when neither inline / file / auto modes are used.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, Tuple


# ---------------------------------------------------------------------------
# GraphQL templates
# ---------------------------------------------------------------------------
#
# Templates use a single ``{limit}`` placeholder; everything else is static.
# Keep each template *minimal* so probes never burn more than ~1 row of credits.

_SOLANA_DEXTRADES = """
query PipelineDataProbe {{
  Solana {{
    DEXTrades(
      limit: {{count: {limit}}}
      orderBy: {{descending: Block_Time}}
    ) {{
      Block {{ Time Slot }}
      Trade {{
        Buy {{ Currency {{ MintAddress }} Amount }}
        Sell {{ Currency {{ MintAddress }} Amount }}
        Dex {{ ProtocolName }}
      }}
    }}
  }}
}}
""".strip()


_SOLANA_TRANSFERS = """
query PipelineDataProbe {{
  Solana {{
    Transfers(
      limit: {{count: {limit}}}
      orderBy: {{descending: Block_Time}}
    ) {{
      Block {{ Time }}
      Transfer {{
        Amount
        Currency {{ MintAddress }}
      }}
    }}
  }}
}}
""".strip()


_SOLANA_BALANCE_UPDATES = """
query PipelineDataProbe {{
  Solana {{
    BalanceUpdates(
      limit: {{count: {limit}}}
      orderBy: {{descending: Block_Time}}
    ) {{
      Block {{ Time }}
      BalanceUpdate {{
        Amount
        Currency {{ MintAddress }}
      }}
    }}
  }}
}}
""".strip()


_SOLANA_TOKEN_HOLDERS = """
query PipelineDataProbe {{
  Solana {{
    TokenHolders(
      limit: {{count: {limit}}}
    ) {{
      Holder {{ Address }}
      Currency {{ MintAddress }}
      Balance {{ Amount }}
    }}
  }}
}}
""".strip()


_SOLANA_WALLET_TOKEN_PNL = """
query PipelineDataProbe {{
  Solana {{
    WalletTokenPnL(
      limit: {{count: {limit}}}
    ) {{
      Wallet {{ Address }}
      Currency {{ MintAddress }}
      RealizedPnL
      UnrealizedPnL
    }}
  }}
}}
""".strip()


_SOLANA_TOKENS = """
query PipelineDataProbe {{
  Solana {{
    Tokens(
      limit: {{count: {limit}}}
    ) {{
      MintAddress
      Name
      Symbol
      Decimals
    }}
  }}
}}
""".strip()


_SOLANA_PAIRS = """
query PipelineDataProbe {{
  Solana {{
    Pairs(
      limit: {{count: {limit}}}
    ) {{
      Pair {{ Address }}
      Token0 {{ MintAddress }}
      Token1 {{ MintAddress }}
      Dex {{ ProtocolName }}
    }}
  }}
}}
""".strip()


_EVM_DEXTRADES = """
query PipelineDataProbe {{
  EVM {{
    DEXTrades(
      limit: {{count: {limit}}}
      orderBy: {{descending: Block_Time}}
    ) {{
      Block {{ Time Number }}
      Trade {{
        Buy {{ Currency {{ SmartContract Symbol }} Amount }}
        Sell {{ Currency {{ SmartContract Symbol }} Amount }}
        Dex {{ ProtocolName }}
      }}
    }}
  }}
}}
""".strip()


_EVM_TRANSFERS = """
query PipelineDataProbe {{
  EVM {{
    Transfers(
      limit: {{count: {limit}}}
      orderBy: {{descending: Block_Time}}
    ) {{
      Block {{ Time }}
      Transfer {{
        Amount
        Currency {{ SmartContract Symbol }}
      }}
    }}
  }}
}}
""".strip()


_EVM_BALANCE_UPDATES = """
query PipelineDataProbe {{
  EVM {{
    BalanceUpdates(
      limit: {{count: {limit}}}
      orderBy: {{descending: Block_Time}}
    ) {{
      Block {{ Time }}
      BalanceUpdate {{
        Amount
        Currency {{ SmartContract Symbol }}
      }}
    }}
  }}
}}
""".strip()


_EVM_TOKENS = """
query PipelineDataProbe {{
  EVM {{
    Tokens(
      limit: {{count: {limit}}}
    ) {{
      SmartContract
      Name
      Symbol
      Decimals
    }}
  }}
}}
""".strip()


_EVM_PAIRS = """
query PipelineDataProbe {{
  EVM {{
    Pairs(
      limit: {{count: {limit}}}
    ) {{
      Pair {{ SmartContract }}
      Token0 {{ SmartContract }}
      Token1 {{ SmartContract }}
      Dex {{ ProtocolName }}
    }}
  }}
}}
""".strip()


# Credit-free fallback. ``__schema`` introspection is always allowed and
# costs zero credits on Bitquery / ChainStream-style endpoints.
_INTROSPECTION_FALLBACK = "{ __schema { queryType { name } } }"


# (chain_group, data_cube) -> template (with ``{limit}`` placeholder)
_TEMPLATES: Dict[Tuple[str, str], str] = {
    ("solana", "DEXTrades"): _SOLANA_DEXTRADES,
    ("solana", "Transfers"): _SOLANA_TRANSFERS,
    ("solana", "BalanceUpdates"): _SOLANA_BALANCE_UPDATES,
    ("solana", "TokenHolders"): _SOLANA_TOKEN_HOLDERS,
    ("solana", "WalletTokenPnL"): _SOLANA_WALLET_TOKEN_PNL,
    ("solana", "Tokens"): _SOLANA_TOKENS,
    ("solana", "Pairs"): _SOLANA_PAIRS,
    ("evm", "DEXTrades"): _EVM_DEXTRADES,
    ("evm", "Transfers"): _EVM_TRANSFERS,
    ("evm", "BalanceUpdates"): _EVM_BALANCE_UPDATES,
    ("evm", "Tokens"): _EVM_TOKENS,
    ("evm", "Pairs"): _EVM_PAIRS,
}


# The default chain_group for "trading" / "defi" / unknown groups.
_TRADING_PREFERRED_CHAIN = "solana"


def _normalize_chain_group(chain_group: str) -> str:
    """Lower-case and re-route synthetic groups (e.g. ``trading``)."""
    cg = (chain_group or "").strip().lower()
    if not cg:
        return "solana"
    if cg in {"trading", "defi", "dex"}:
        return _TRADING_PREFERRED_CHAIN
    return cg


def build_probe_query(
    chain_group: str,
    data_cube: str,
    *,
    limit: int = 1,
) -> str:
    """Build a minimal GraphQL probe query for ``(chain_group, data_cube)``.

    Returns an introspection query (zero-credit) for unknown combinations.
    """
    if limit < 1:
        limit = 1

    cg = _normalize_chain_group(chain_group)
    cube = (data_cube or "").strip() or "DEXTrades"

    template = _TEMPLATES.get((cg, cube))
    if template is None:
        return _INTROSPECTION_FALLBACK
    return template.format(limit=limit)


# ---------------------------------------------------------------------------
# Config-driven selection
# ---------------------------------------------------------------------------

def _chainstream_fit(config: dict) -> dict:
    if not isinstance(config, dict):
        return {}
    pba = config.get("pre_build_analysis")
    if not isinstance(pba, dict):
        return {}
    fit = pba.get("chainstream_fit")
    return fit if isinstance(fit, dict) else {}


def _first_str(values, default: str) -> str:
    if isinstance(values, list):
        for item in values:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return default


def select_probe_target(config: dict) -> Tuple[str, str, str]:
    """Pick ``(chain_group, data_cube, query_template)`` from a case config.

    Falls back to ``("solana", "DEXTrades")`` when the case config is missing
    the relevant ``chainstream_fit`` hints.
    """
    fit = _chainstream_fit(config)
    chain_group = _first_str(fit.get("chain_groups"), "solana")
    data_cube = _first_str(fit.get("data_cubes"), "DEXTrades")
    query = build_probe_query(chain_group, data_cube)
    return chain_group, data_cube, query


def describe_probe_target(chain_group: str, data_cube: str) -> str:
    """Return the canonical ``query_source`` label for a probe target."""
    cg = (chain_group or "solana").strip() or "solana"
    cube = (data_cube or "DEXTrades").strip() or "DEXTrades"
    return f"chainstream:{cg}.{cube}"


# ---------------------------------------------------------------------------
# argparse integration
# ---------------------------------------------------------------------------

def register_query_builder_args(parser: argparse.ArgumentParser) -> None:
    """Register the auto-build CLI flags on ``parser``.

    Idempotent if the flags already exist (re-registration raises argparse's
    own ``ArgumentError`` — callers are expected to wire this exactly once).
    """
    parser.add_argument(
        "--chainstream-auto-build-query",
        action="store_true",
        default=False,
        help=(
            "Dynamically build the data-probe GraphQL query from "
            "pre_build_analysis.chainstream_fit (chain_groups[0], "
            "data_cubes[0]). Ignored when --chainstream-query or "
            "--chainstream-query-file is also provided."
        ),
    )
    parser.add_argument(
        "--chainstream-probe-chain-group",
        default="",
        help=(
            "Override the chain_group used by --chainstream-auto-build-query "
            "(e.g. 'solana', 'evm')."
        ),
    )
    parser.add_argument(
        "--chainstream-probe-data-cube",
        default="",
        help=(
            "Override the data_cube used by --chainstream-auto-build-query "
            "(e.g. 'DEXTrades', 'Transfers', 'WalletTokenPnL')."
        ),
    )


# ---------------------------------------------------------------------------
# Top-level resolver
# ---------------------------------------------------------------------------

# The original default string from ``run_pipeline.DEFAULT_CHAINSTREAM_GRAPHQL_QUERY``.
# Re-declared here so this module is importable without ``run_pipeline``.
DEFAULT_CHAINSTREAM_GRAPHQL_QUERY = build_probe_query("solana", "DEXTrades")
DEFAULT_QUERY_SOURCE = "default_solana_dextrades"


def resolve_probe_query(
    args: argparse.Namespace,
    config: dict,
) -> Tuple[str, str]:
    """Resolve ``(query, query_source)`` for the data-probe.

    Priority:
      1. ``--chainstream-query`` (inline)            -> ``"inline"``
      2. ``--chainstream-query-file`` (file path)    -> ``str(path)``
      3. ``--chainstream-auto-build-query``          -> ``"chainstream:<cg>.<cube>"``
      4. Default Solana DEXTrades                    -> ``"default_solana_dextrades"``
    """
    inline = getattr(args, "chainstream_query", "") or ""
    if inline:
        return inline, "inline"

    query_file = getattr(args, "chainstream_query_file", "") or ""
    if query_file:
        path = Path(query_file).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"ChainStream query file not found: {path}")
        return path.read_text(encoding="utf-8"), str(path)

    auto = bool(getattr(args, "chainstream_auto_build_query", False))
    if auto:
        fit = _chainstream_fit(config)
        chain_group = (
            getattr(args, "chainstream_probe_chain_group", "") or ""
        ).strip() or _first_str(fit.get("chain_groups"), "solana")
        data_cube = (
            getattr(args, "chainstream_probe_data_cube", "") or ""
        ).strip() or _first_str(fit.get("data_cubes"), "DEXTrades")
        query = build_probe_query(chain_group, data_cube)
        return query, describe_probe_target(chain_group, data_cube)

    return DEFAULT_CHAINSTREAM_GRAPHQL_QUERY, DEFAULT_QUERY_SOURCE


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    fake_config = {
        "pre_build_analysis": {
            "chainstream_fit": {
                "chain_groups": ["evm", "trading"],
                "data_cubes": ["DEXTrades", "Tokens"],
                "target_capability": "graphql",
                "query_intent": "Recent EVM DEX trades for sample mint.",
            }
        }
    }
    cg, cube, q = select_probe_target(fake_config)
    print(f"chain_group = {cg}")
    print(f"data_cube   = {cube}")
    print(f"query_source= {describe_probe_target(cg, cube)}")
    print("---- query (head) ----")
    head = "\n".join(q.splitlines()[:10])
    print(head)
    print("---- introspection fallback ----")
    print(build_probe_query("foo", "bar"))
