"""Pluggable data-source protocol for the agentflow pipeline.

Historically the pipeline assumed *ChainStream* as a first-class data source:
its keyword dictionaries, GraphQL probe templates, gate-yaml field names
(``pre_build_analysis.chainstream_fit``) and credentials env vars
(``CHAINSTREAM_API_KEY``) were all hard-coded in :mod:`cli`.

This module abstracts that into a :class:`DataSourcePlugin` Protocol so
alternative data sources (Bitquery, The Graph, Dune, internal lakes …) can
be plugged in without touching ``cli.py``. The default plugin —
:class:`ChainStreamDataSource` — preserves *byte-for-byte* the original
ChainStream behaviour, so existing case yaml files keep working with zero
edits (gate field stays ``chainstream_fit``).

A second example plugin :class:`BitqueryDataSource` ships in this module to
prove the protocol is genuinely pluggable; it deliberately keeps the same
GraphQL shape as ChainStream so swapping the plugin is a one-flag change.

Pure stdlib + ``typing.Protocol``.
"""
from __future__ import annotations

import json
import os
from typing import Protocol, runtime_checkable
from urllib import error, parse, request


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DataSourceError(RuntimeError):
    """Raised for data-source plugin errors (registration / probe / shape)."""


# ---------------------------------------------------------------------------
# Helpers (kept private — duplicated from cli.dedupe_terms intentionally so
# this module has zero import dependency on cli to avoid circular imports)
# ---------------------------------------------------------------------------


def _dedupe_terms(parts: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        normalized = (part or "").strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _candidate_text(candidate: dict, *, include_homepage: bool = True) -> str:
    parts = [
        str(candidate.get("name", "")),
        str(candidate.get("description", "")),
        str(candidate.get("fit_reason", "")),
    ]
    if include_homepage:
        parts.append(str(candidate.get("homepage", "")))
    return " ".join(parts).lower()


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class DataSourcePlugin(Protocol):
    """Abstract data-source plugin contract.

    Implementations describe *one* upstream data platform (ChainStream,
    Bitquery, The Graph, Dune, internal warehouse, …) for the pipeline's
    pre-build analysis + data-probe gate.

    Required attributes:

    ``name``
        Stable plugin identifier used by the registry / CLI flag, e.g.
        ``"chainstream"``, ``"bitquery"``.

    ``gate_field``
        Name of the dict key the pipeline writes under
        ``pre_build_analysis.<gate_field>`` in the case yaml. Defaults to
        ``"chainstream_fit"`` for the canonical ChainStream plugin so
        existing yaml files stay valid.

    Required methods are mirrored on :class:`ChainStreamDataSource` and
    documented there.
    """

    name: str
    gate_field: str

    def assess_fit(self, candidate: dict) -> tuple[int, str, str]: ...

    def infer_targets(self, candidate: dict) -> dict: ...

    def keyword_dict(self) -> dict[str, list[str]]: ...

    def default_probe_query(self) -> str: ...

    def build_probe_query(
        self, chain_group: str, data_cube: str, *, limit: int = 1
    ) -> str: ...

    def select_probe_target(self, config: dict) -> tuple[str, str, str]: ...

    def post_graphql_probe(
        self, endpoint: str, api_key: str, query: str
    ) -> dict: ...


# ---------------------------------------------------------------------------
# ChainStream implementation (default — matches the original cli.py logic)
# ---------------------------------------------------------------------------


class ChainStreamDataSource:
    """Default plugin — preserves the exact pre-refactor ChainStream behaviour.

    The :func:`assess_fit` and :func:`infer_targets` algorithms are copied
    verbatim from the original ``cli.assess_chainstream_fit`` /
    ``cli.infer_chainstream_targets`` so all existing yaml fixtures stay
    bit-for-bit compatible.
    """

    name = "chainstream"
    gate_field = "chainstream_fit"

    # Languages the score_candidate keyword dict gives an SDK-friendly bonus.
    friendly_languages: tuple[str, ...] = (
        "typescript",
        "javascript",
        "python",
        "go",
        "rust",
        "java",
        "kotlin",
    )

    # ------------------------------------------------------------------
    # Keyword scoring
    # ------------------------------------------------------------------

    def keyword_dict(self) -> dict[str, list[str]]:
        """Domain keyword dictionary used by ``score_candidate``.

        Keys are *category labels*, values are lower-cased substrings to
        scan against the candidate text. Categories:

        ``access``   transport / API style hints
        ``domain``   on-chain / DeFi domain hints
        ``product``  data-product shape hints
        """
        return {
            "access": [
                "graphql",
                "kafka",
                "websocket",
                "stream",
                "api",
                "sdk",
            ],
            "domain": [
                "dex",
                "trade",
                "token",
                "wallet",
                "pnl",
                "defi",
                "onchain",
                "blockchain",
                "solana",
                "ethereum",
                "evm",
                "polygon",
            ],
            "product": [
                "analytics",
                "dashboard",
                "monitor",
                "alert",
                "indexer",
                "pipeline",
                "subgraph",
            ],
            "friendly_languages": list(self.friendly_languages),
        }

    # ------------------------------------------------------------------
    # Fit assessment (ported verbatim from cli.assess_chainstream_fit)
    # ------------------------------------------------------------------

    def assess_fit(self, candidate: dict) -> tuple[int, str, str]:
        text = _candidate_text(candidate, include_homepage=True)
        language = str(candidate.get("language", "")).lower()
        score = 0
        reasons: list[str] = []
        access_method = "graphql"

        access_terms = {
            "graphql": "GraphQL",
            "kafka": "Kafka",
            "websocket": "WebSocket",
            "stream": "streaming",
            "api": "API",
            "sdk": "SDK",
        }
        matched_access = [
            label for term, label in access_terms.items() if term in text
        ]
        if matched_access:
            score += 20
            reasons.append(
                f"候选项目已出现 {'/'.join(matched_access[:2])} 相关能力"
            )
            if "Kafka" in matched_access or "streaming" in matched_access:
                access_method = "kafka"
            elif "WebSocket" in matched_access:
                access_method = "websocket"

        domain_terms = {
            "dex": "DEX",
            "trade": "交易",
            "token": "token",
            "wallet": "wallet",
            "pnl": "PnL",
            "defi": "DeFi",
            "onchain": "on-chain",
            "blockchain": "blockchain",
            "solana": "Solana",
            "ethereum": "Ethereum",
            "evm": "EVM",
            "polygon": "Polygon",
        }
        matched_domain = [
            label for term, label in domain_terms.items() if term in text
        ]
        if matched_domain:
            score += min(35, 10 + len(matched_domain) * 5)
            reasons.append(
                f"数据域贴近 Chainstream 覆盖范围: {'/'.join(matched_domain[:4])}"
            )

        product_terms = {
            "analytics": "analytics",
            "dashboard": "dashboard",
            "monitor": "monitor",
            "alert": "alert",
            "indexer": "indexer",
            "pipeline": "pipeline",
            "subgraph": "subgraph",
        }
        matched_product = [
            label for term, label in product_terms.items() if term in text
        ]
        if matched_product:
            score += min(25, 10 + len(matched_product) * 5)
            reasons.append(
                f"项目形态适合数据产品: {'/'.join(matched_product[:3])}"
            )

        if "chainstream" in text:
            score += 15
            reasons.append("已直接提到 chainstream")

        if language in {"typescript", "javascript", "python"}:
            score += 5
            reasons.append(
                f"主语言 {language} 与 Chainstream SDK/REST/GraphQL 客户端生态契合"
            )
        elif language in {"go", "rust", "java", "kotlin"}:
            score += 3
            reasons.append(f"主语言 {language} 适合接 Kafka/streaming")
            if access_method == "graphql" and "stream" not in text and "kafka" not in text:
                access_method = "kafka" if language in {"go", "java", "kotlin"} else access_method

        if "graphql" in text:
            score += 10
            access_method = "graphql"
        if "kafka" in text:
            score += 10
            access_method = "kafka"

        if candidate.get("is_archived"):
            score = max(0, score - 25)
            reasons.append("仓库已归档，扣分")

        if not reasons:
            reasons.append("未从候选仓库描述中看到明确的 Chainstream 数据适配信号")
        return min(score, 100), "，".join(reasons), access_method

    # ------------------------------------------------------------------
    # Target inference (ported verbatim from cli.infer_chainstream_targets)
    # ------------------------------------------------------------------

    def infer_targets(self, candidate: dict) -> dict:
        text = _candidate_text(candidate, include_homepage=False)
        chain_groups: list[str] = []
        data_cubes: list[str] = []

        if any(term in text for term in ("solana", "raydium", "orca", "mint")):
            chain_groups.append("solana")
        if any(term in text for term in ("ethereum", "evm", "polygon", "bsc", "bnb")):
            chain_groups.append("evm")
        if any(term in text for term in ("ohlc", "price", "market", "token")):
            chain_groups.append("trading")

        cube_terms = {
            "DEXTrades": ("dex", "swap", "trade", "trading"),
            "DEXTradeByTokens": ("token trade", "token trades", "token analytics"),
            "Transfers": ("transfer", "token movement"),
            "BalanceUpdates": ("balance", "wallet balance"),
            "TokenHolders": ("holder", "holders"),
            "WalletTokenPnL": ("pnl", "profit", "loss"),
            "Tokens": ("token", "market cap", "volume"),
            "Pairs": ("ohlc", "candle", "price chart"),
            "Transactions": ("transaction", "tx"),
            "Events": ("event", "contract log"),
        }
        for cube, terms in cube_terms.items():
            if any(term in text for term in terms):
                data_cubes.append(cube)

        access = str(
            candidate.get("recommended_chainstream_access", "") or "graphql"
        )
        if access == "kafka":
            latency_need = "near_realtime"
            query_intent = (
                "Consume event streams for monitoring, alerts, or indexing."
            )
        elif access == "websocket":
            latency_need = "realtime"
            query_intent = (
                "Subscribe to live updates for a UI or alert workflow."
            )
        else:
            latency_need = "analytical"
            query_intent = "Run analytical GraphQL queries over Chainstream cubes."

        aggregation_need = (
            "heavy"
            if any(
                term in text
                for term in ("analytics", "dashboard", "volume", "pnl", "aggregate")
            )
            else "light"
        )
        refs = ["https://docs.chainstream.io/en/docs/access-methods/overview"]
        if access == "graphql":
            refs.append("https://docs.chainstream.io/en/graphql/getting-started/overview")
            refs.append("https://docs.chainstream.io/en/graphql/schema/cubes")
        elif access == "kafka":
            refs.append("https://docs.chainstream.io/en/docs/access-methods/overview")

        return {
            "chain_groups": _dedupe_terms(chain_groups),
            "data_cubes": _dedupe_terms(data_cubes),
            "query_intent": query_intent,
            "aggregation_need": aggregation_need,
            "latency_need": latency_need,
            "api_doc_refs": _dedupe_terms(refs),
        }

    # ------------------------------------------------------------------
    # Probe queries — delegate to the existing chainstream_query_builder
    # so we don't duplicate the ~12 GraphQL templates.
    # ------------------------------------------------------------------

    def default_probe_query(self) -> str:
        from .chainstream_query_builder import DEFAULT_CHAINSTREAM_GRAPHQL_QUERY

        return DEFAULT_CHAINSTREAM_GRAPHQL_QUERY

    def build_probe_query(
        self, chain_group: str, data_cube: str, *, limit: int = 1
    ) -> str:
        from .chainstream_query_builder import build_probe_query as _bpq

        return _bpq(chain_group, data_cube, limit=limit)

    def select_probe_target(self, config: dict) -> tuple[str, str, str]:
        from .chainstream_query_builder import select_probe_target as _spt

        return _spt(config)

    # ------------------------------------------------------------------
    # GraphQL probe HTTP — copied from cli.post_chainstream_graphql so the
    # plugin owns its own auth header convention (X-API-KEY for ChainStream)
    # ------------------------------------------------------------------

    def post_graphql_probe(
        self, endpoint: str, api_key: str, query: str
    ) -> dict:
        return _post_graphql(
            endpoint,
            query,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            shape_error="Unexpected ChainStream GraphQL response shape.",
        )


# ---------------------------------------------------------------------------
# Bitquery example implementation — proves the protocol is pluggable
# ---------------------------------------------------------------------------


class BitqueryDataSource:
    """Minimal Bitquery (https://bitquery.io) plugin example.

    Bitquery's public GraphQL endpoint exposes a schema close to
    ChainStream's, so we reuse the same probe templates (DEXTrades / Tokens
    / Pairs / …). The differences this plugin highlights:

    * ``gate_field`` writes to ``pre_build_analysis.bitquery_fit`` instead of
      ``chainstream_fit`` — the host pipeline must read ``plugin.gate_field``
      everywhere it touches the gate yaml.
    * Auth uses an ``Authorization: Bearer`` header (not ``X-API-KEY``).
    * Default endpoint is ``https://graphql.bitquery.io/`` — the host CLI
      exposes ``--chainstream-endpoint`` for this even though the plugin
      may belong to a different vendor (kept generic on purpose to avoid a
      churn of CLI flag renames).

    The keyword dictionary is intentionally a strict superset of
    ChainStream's so cross-plugin scoring stays comparable.
    """

    name = "bitquery"
    gate_field = "bitquery_fit"

    # Reuse the same friendly language list — Bitquery clients exist for
    # the same SDK ecosystems.
    friendly_languages: tuple[str, ...] = ChainStreamDataSource.friendly_languages

    def keyword_dict(self) -> dict[str, list[str]]:
        kw = ChainStreamDataSource().keyword_dict()
        # Bitquery covers more L1/L2 chains than ChainStream's keyword set;
        # add a couple of representative ones to show keyword extensibility.
        kw["domain"] = list(kw["domain"]) + ["arbitrum", "optimism", "tron"]
        return kw

    def assess_fit(self, candidate: dict) -> tuple[int, str, str]:
        # Reuse the ChainStream scoring as the baseline — Bitquery's
        # universe is a near-superset, so the same signals apply. We only
        # add a small bonus when the candidate explicitly mentions Bitquery.
        score, reason, access = ChainStreamDataSource().assess_fit(candidate)
        text = _candidate_text(candidate)
        if "bitquery" in text:
            score = min(100, score + 15)
            reason = (
                f"{reason}，已直接提到 bitquery"
                if reason
                else "已直接提到 bitquery"
            )
        return score, reason, access

    def infer_targets(self, candidate: dict) -> dict:
        targets = ChainStreamDataSource().infer_targets(candidate)
        # Override the doc references — Bitquery ships its own docs.
        targets["api_doc_refs"] = [
            "https://docs.bitquery.io/",
            "https://docs.bitquery.io/docs/category/graphql-api/",
        ]
        return targets

    def default_probe_query(self) -> str:
        # Same shape as ChainStream's default — Bitquery exposes Solana
        # DEXTrades with a compatible field set.
        return ChainStreamDataSource().default_probe_query()

    def build_probe_query(
        self, chain_group: str, data_cube: str, *, limit: int = 1
    ) -> str:
        return ChainStreamDataSource().build_probe_query(
            chain_group, data_cube, limit=limit
        )

    def select_probe_target(self, config: dict) -> tuple[str, str, str]:
        # Read from this plugin's own gate field rather than ``chainstream_fit``.
        fit = {}
        if isinstance(config, dict):
            pba = config.get("pre_build_analysis")
            if isinstance(pba, dict) and isinstance(pba.get(self.gate_field), dict):
                fit = pba[self.gate_field]

        chain_groups = fit.get("chain_groups") if isinstance(fit, dict) else None
        data_cubes = fit.get("data_cubes") if isinstance(fit, dict) else None

        chain_group = "solana"
        if isinstance(chain_groups, list):
            for item in chain_groups:
                if isinstance(item, str) and item.strip():
                    chain_group = item.strip()
                    break

        data_cube = "DEXTrades"
        if isinstance(data_cubes, list):
            for item in data_cubes:
                if isinstance(item, str) and item.strip():
                    data_cube = item.strip()
                    break

        query = self.build_probe_query(chain_group, data_cube)
        return chain_group, data_cube, query

    def post_graphql_probe(
        self, endpoint: str, api_key: str, query: str
    ) -> dict:
        return _post_graphql(
            endpoint,
            query,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            shape_error="Unexpected Bitquery GraphQL response shape.",
        )


# ---------------------------------------------------------------------------
# Internal HTTP helper (avoid circular import with cli.http_json)
# ---------------------------------------------------------------------------


def _post_graphql(
    endpoint: str,
    query: str,
    *,
    headers: dict[str, str],
    shape_error: str,
) -> dict:
    body = json.dumps({"query": query}).encode("utf-8")
    final_headers = {
        "Accept": "application/json",
        "User-Agent": "agentflow-git-repo-clone/0.1",
        **headers,
    }
    req = request.Request(endpoint, data=body, headers=final_headers, method="POST")
    try:
        with request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise DataSourceError(
            f"HTTP {exc.code} from {endpoint}: {detail}"
        ) from exc
    except error.URLError as exc:
        raise DataSourceError(f"Unable to reach {endpoint}: {exc.reason}") from exc
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise DataSourceError(
            f"Unable to parse JSON response from {endpoint}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DataSourceError(shape_error)
    return payload


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, DataSourcePlugin] = {}
_DEFAULT_NAME = "chainstream"


def register_data_source(plugin: DataSourcePlugin) -> None:
    """Register ``plugin`` under its ``name`` (overwrites any existing entry).

    Raises :class:`DataSourceError` if ``plugin.name`` is missing/empty or
    if ``plugin`` does not satisfy :class:`DataSourcePlugin`.
    """
    if not isinstance(plugin, DataSourcePlugin):
        raise DataSourceError(
            f"Plugin {plugin!r} does not satisfy DataSourcePlugin protocol."
        )
    name = getattr(plugin, "name", "") or ""
    if not name.strip():
        raise DataSourceError("DataSourcePlugin.name must be a non-empty string.")
    _REGISTRY[name.strip()] = plugin


def get_data_source(name: str) -> DataSourcePlugin:
    """Look up a registered plugin by name. Raises ``DataSourceError`` if missing."""
    key = (name or "").strip()
    if not key:
        raise DataSourceError("Empty data-source name.")
    if key not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise DataSourceError(
            f"Unknown data source: {key!r}. Available: {available}."
        )
    return _REGISTRY[key]


def default_data_source() -> DataSourcePlugin:
    """Return the plugin selected by ``AGENTFLOW_DATA_SOURCE`` (or chainstream).

    Reading the env var lazily on each call lets tests flip it via
    ``monkeypatch.setenv`` without re-importing the module.
    """
    requested = os.environ.get("AGENTFLOW_DATA_SOURCE", "").strip() or _DEFAULT_NAME
    return get_data_source(requested)


def registered_data_sources() -> list[str]:
    """Return the sorted list of registered plugin names (mostly for tests / CLI help)."""
    return sorted(_REGISTRY.keys())


# Pre-register the two built-in plugins at import time so callers that just
# do ``from agentflow_pipeline.data_source import default_data_source`` get a
# usable registry without needing setup boilerplate.
register_data_source(ChainStreamDataSource())
register_data_source(BitqueryDataSource())


__all__ = [
    "BitqueryDataSource",
    "ChainStreamDataSource",
    "DataSourceError",
    "DataSourcePlugin",
    "default_data_source",
    "get_data_source",
    "register_data_source",
    "registered_data_sources",
]
