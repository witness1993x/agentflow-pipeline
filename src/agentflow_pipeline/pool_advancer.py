"""Pool-level auto-advance: per-case readiness-aware mode selection.

Where ``pool_runner.run_pool_parallel`` runs the *same* ``--pool-mode`` for
every case, this module decides the *next* per-case mode based on the
existing ``execution_state`` snapshot inside each ``02-pipeline-gate.yaml``.

Decision tree (see :func:`next_mode_for`)
-----------------------------------------
1. ``decision.final_status == "drop"`` -> ``None`` (case is dropped).
2. ``execution_state.publish_readiness.status``:
   - ``ready``    -> ``None`` (or ``"publish"`` iff ``include_publish=True``).
   - ``published``-> ``None``.
   - ``blocked_*``-> ``None`` (a human must unblock; auto-advance refuses).
3. ``execution_state.discovery.last_run_at`` empty -> ``"discover"``.
4. ``execution_state.data_probe.status`` empty AND
   ``gate_3_repo_routing.repo_strategy != "undecided"`` -> ``"data-probe"``.
5. ``execution_state.probe.last_run_at`` empty AND
   ``execution_state.data_probe.status == "passed"`` -> ``"probe"``.
6. Anything else / ambiguous -> ``None`` (fail-closed; we never guess).

The orchestrator :func:`run_pool_auto_advance` repeatedly:
  - reads each case's yaml,
  - groups cases by their ``next_mode_for(...)`` answer,
  - launches one ``pool_runner.run_pool_parallel(group, mode=...)`` per group,
  - re-reads the yamls for the next round,

up to ``max_rounds`` rounds, stopping early once every case answers ``None``.

Integration patch into ``run_pipeline.py`` (NOT applied automatically)
----------------------------------------------------------------------
This module deliberately does NOT edit ``run_pipeline.py`` or
``pool_runner.py``. To wire it in, apply::

    # Top of run_pipeline.py, alongside the other pool imports:
    from pool_advancer import (
        register_advance_args,
        run_pool_auto_advance,
        next_mode_for,
    )

    # At the end of parse_args() in run_pipeline.py, after register_pool_args:
    register_advance_args(parser)

    # Inside _run_pool_branch() in run_pipeline.py, BEFORE the existing
    # ``run_pool_parallel`` call, branch on the new flag::

        if getattr(args, "pool_auto_advance", False):
            report = run_pool_auto_advance(
                cases,
                max_workers=kwargs["max_workers"],
                run_pipeline_script=Path(__file__).resolve(),
                timeout_per_case=kwargs["timeout_per_case"],
                extra_args=kwargs.get("extra_args", []),
                max_rounds=int(getattr(args, "pool_auto_advance_max_rounds", 3)),
                include_publish=bool(getattr(
                    args, "pool_auto_advance_include_publish", False
                )),
                on_round_complete=lambda rr: print(
                    f"  round {rr['round']} done: "
                    f"groups={list(rr['groups'].keys())}"
                ),
            )
            print()
            print(format_advance_summary(report))
            stuck = report.get("stuck_cases", [])
            return 0 if not stuck else 1
        # ... fall through to the existing fixed-mode pool_runner branch.

Why default-off for ``publish``?
--------------------------------
``run_pipeline.py`` already forbids ``mode=publish`` from the pool runner,
and publishing requires both ``--execute`` and ``--allow-publish``. Auto
advancing into publish would silently mass-publish every case that happens
to land at ``ready``. We keep it gated behind an explicit
``--pool-auto-advance-include-publish`` flag so the default behaviour is
"prepare to publish, then stop and wait for human approval".
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Callable

try:
    import yaml  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - yaml is a hard pipeline dep
    yaml = None  # type: ignore[assignment]

from .pool_runner import run_pool_parallel

logger = logging.getLogger(__name__)


# Modes ``next_mode_for`` may legitimately recommend. ``"publish"`` is only
# returned when the caller explicitly opts in via ``include_publish=True``.
_ADVANCEABLE_MODES: tuple[str, ...] = ("discover", "data-probe", "probe")
_BLOCKED_PREFIX = "blocked_"


# --------------------------------------------------------------------------- #
# Yaml helpers (best-effort; never raise on missing keys)
# --------------------------------------------------------------------------- #
def _load_gate(case_dir: Path) -> dict | None:
    """Load ``case_dir/02-pipeline-gate.yaml`` or return ``None`` on error.

    Used by :func:`run_pool_auto_advance` to re-read yaml between rounds.
    Standalone callers usually pass a config dict directly.
    """
    if yaml is None:
        return None
    gate = case_dir / "02-pipeline-gate.yaml"
    if not gate.exists():
        return None
    try:
        with gate.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):  # type: ignore[attr-defined]
        return None
    return data if isinstance(data, dict) else None


def _get_str(config: dict, *path: str) -> str:
    """Walk ``config`` along ``path`` and coerce the leaf to ``str`` (or '')."""
    cur: Any = config
    for key in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
        if cur is None:
            return ""
    if cur is None:
        return ""
    if isinstance(cur, str):
        return cur
    return str(cur)


# --------------------------------------------------------------------------- #
# Core decision
# --------------------------------------------------------------------------- #
def next_mode_for(config: dict, *, include_publish: bool = False) -> str | None:
    """Decide the next ``--mode`` to run for ``config``, or ``None`` to skip.

    Parameters
    ----------
    config:
        Parsed ``02-pipeline-gate.yaml`` dictionary.
    include_publish:
        When True and ``publish_readiness.status == "ready"``, the function
        returns ``"publish"`` instead of ``None``. Default False (safe).

    Returns
    -------
    One of ``{"discover", "data-probe", "probe", "publish", None}``.
    Returns ``None`` whenever the case is finished, dropped, blocked, or in
    any state the heuristic does not understand (fail-closed).
    """
    if not isinstance(config, dict):
        return None

    # 0. Dropped cases never advance.
    final_status = _get_str(config, "decision", "final_status")
    if final_status == "drop":
        return None

    # 1. Terminal / blocked readiness states win over anything else.
    readiness = _get_str(config, "execution_state", "publish_readiness", "status")
    if readiness == "published":
        return None
    if readiness == "ready":
        return "publish" if include_publish else None
    if readiness.startswith(_BLOCKED_PREFIX):
        # blocked_data_probe / blocked_kafka_probe / blocked_buildability:
        # a human must intervene. Auto-advance refuses to spin on a wall.
        return None

    # 2. Discovery gate.
    discovery_last = _get_str(
        config, "execution_state", "discovery", "last_run_at"
    )
    if not discovery_last:
        return "discover"

    # 3. Data-probe gate. We only run data-probe once a routing decision
    # exists (``repo_strategy != "undecided"``); otherwise we'd probe the
    # wrong thing.
    repo_strategy = _get_str(config, "gate_3_repo_routing", "repo_strategy")
    data_probe_status = _get_str(
        config, "execution_state", "data_probe", "status"
    )
    if not data_probe_status and repo_strategy and repo_strategy != "undecided":
        return "data-probe"

    # 4. Build-probe gate -- only after data-probe passed.
    probe_last = _get_str(config, "execution_state", "probe", "last_run_at")
    if not probe_last and data_probe_status == "passed":
        return "probe"

    # 5. ``in_progress`` / ``not_started`` with everything filled in: we
    # have nothing safe to run. Bail.
    return None


def describe_advance_decision(
    config: dict, *, include_publish: bool = False
) -> str:
    """Return a one-line Chinese explanation of the auto-advance decision.

    Used for logging so operators can read why a case was advanced -- or
    why it was skipped -- without re-deriving the rules.
    """
    if not isinstance(config, dict):
        return "配置无效，跳过"

    final_status = _get_str(config, "decision", "final_status")
    if final_status == "drop":
        return "case 已 drop，跳过自动推进"

    readiness = _get_str(config, "execution_state", "publish_readiness", "status")
    if readiness == "published":
        return "已 published，无需推进"
    if readiness == "ready":
        if include_publish:
            return "已 ready 且开启了 include_publish，准备推 publish"
        return "已 ready 等待人工 publish 决策"
    if readiness == "blocked_data_probe":
        return "data-probe 阻塞，需人工解决数据访问后再推"
    if readiness == "blocked_kafka_probe":
        return "kafka-probe 阻塞，需人工解决 Kafka 接入后再推"
    if readiness == "blocked_buildability":
        return "buildability 阻塞，需人工修复构建后再推"

    discovery_last = _get_str(
        config, "execution_state", "discovery", "last_run_at"
    )
    if not discovery_last:
        return "discovery 尚未执行，需先跑 discover"

    repo_strategy = _get_str(config, "gate_3_repo_routing", "repo_strategy")
    data_probe_status = _get_str(
        config, "execution_state", "data_probe", "status"
    )
    if not data_probe_status:
        if repo_strategy and repo_strategy != "undecided":
            return "discovery 已完成，data-probe 未跑，可推 data-probe"
        return "discovery 已完成但 repo_strategy 仍 undecided，等待路由决策"

    probe_last = _get_str(config, "execution_state", "probe", "last_run_at")
    if data_probe_status == "passed" and not probe_last:
        return "data-probe 已 pass，可推 build probe"
    if data_probe_status in {"failed", "blocked"}:
        return "data-probe 未 pass，不自动推 build probe"

    return "状态不明，保守起见跳过"


# --------------------------------------------------------------------------- #
# Top-level orchestrator
# --------------------------------------------------------------------------- #
def run_pool_auto_advance(
    cases: list[Path],
    *,
    max_workers: int = 4,
    run_pipeline_script: Path,
    timeout_per_case: int = 300,
    extra_args: list[str] | None = None,
    max_rounds: int = 3,
    include_publish: bool = False,
    on_round_complete: Callable[[dict], None] | None = None,
) -> dict:
    """Drive ``cases`` forward, one readiness-step per round, up to ``max_rounds``.

    Each round:
      1. For every case, re-read its yaml and call :func:`next_mode_for`.
      2. Group cases sharing the same recommended mode.
      3. Submit each group to :func:`pool_runner.run_pool_parallel`.
      4. After all groups in the round complete, optionally invoke
         ``on_round_complete(round_report)``.

    Stops early when every remaining case answers ``None``. Cases that keep
    answering ``None`` (because they are blocked, ready, dropped, ambiguous,
    or cannot be parsed) are reported in ``stuck_cases``.

    Returns
    -------
    dict
        ``{
            "rounds": [round_report, ...],
            "total_cases": int,
            "advanced_cases": int,        # cases that ran at least once
            "rounds_completed": int,
            "stuck_cases": [{"case_dir": str, "stuck_at": str}, ...],
        }``
    """
    extras = list(extra_args or [])
    rounds: list[dict] = []
    advanced: set[str] = set()
    case_paths = [Path(c) for c in cases]
    last_decisions: dict[str, str] = {}

    for round_idx in range(1, max_rounds + 1):
        # Re-read every yaml so we see the *current* execution_state after
        # whatever the previous round wrote back.
        groups: dict[str, list[Path]] = {}
        per_case_decision: dict[str, str | None] = {}
        skip_reasons: dict[str, str] = {}

        for case in case_paths:
            cfg = _load_gate(case)
            if cfg is None:
                per_case_decision[str(case)] = None
                skip_reasons[str(case)] = "yaml 不可读，跳过"
                continue
            decision = next_mode_for(cfg, include_publish=include_publish)
            per_case_decision[str(case)] = decision
            if decision is None:
                skip_reasons[str(case)] = describe_advance_decision(
                    cfg, include_publish=include_publish
                )
                continue
            groups.setdefault(decision, []).append(case)

        # Remember the *latest* skip reason per case for stuck reporting.
        for k, v in skip_reasons.items():
            last_decisions[k] = v

        round_report: dict[str, Any] = {
            "round": round_idx,
            "groups": {},
            "skipped": {k: v for k, v in skip_reasons.items()},
        }

        if not groups:
            # Nothing left to do. Record an empty round and bail.
            rounds.append(round_report)
            if on_round_complete is not None:
                try:
                    on_round_complete(round_report)
                except Exception:  # noqa: BLE001 - callback isolation
                    logger.exception("on_round_complete raised; ignoring")
            break

        for mode, group_cases in groups.items():
            if mode == "publish":
                # Sanity guard: pool_runner.run_pool_parallel forbids
                # publish. If a caller really opted in, they need a
                # different code path. We refuse here too.
                logger.warning(
                    "auto-advance refused to dispatch %d case(s) to publish "
                    "(pool runner forbids it); flag them as advanced anyway",
                    len(group_cases),
                )
                round_report["groups"][mode] = {
                    "total": len(group_cases),
                    "passed": 0,
                    "failed": 0,
                    "timeout": 0,
                    "duration_seconds": 0.0,
                    "results": [
                        {
                            "case_dir": str(c),
                            "mode": mode,
                            "status": "failed",
                            "returncode": -10,
                            "stderr_tail": "publish refused by pool runner",
                            "stdout_tail": "",
                            "duration_seconds": 0.0,
                        }
                        for c in group_cases
                    ],
                }
                continue
            sub_report = run_pool_parallel(
                group_cases,
                mode=mode,
                max_workers=max_workers,
                extra_args=extras,
                run_pipeline_script=run_pipeline_script,
                timeout_per_case=timeout_per_case,
            )
            round_report["groups"][mode] = sub_report
            for c in group_cases:
                advanced.add(str(c))

        rounds.append(round_report)
        if on_round_complete is not None:
            try:
                on_round_complete(round_report)
            except Exception:  # noqa: BLE001 - callback isolation
                logger.exception("on_round_complete raised; ignoring")

    # Final pass: cases that *ended* with no recommended mode are "stuck".
    stuck: list[dict] = []
    for case in case_paths:
        cfg = _load_gate(case)
        decision = (
            next_mode_for(cfg, include_publish=include_publish)
            if cfg is not None
            else None
        )
        if decision is None:
            reason = (
                describe_advance_decision(cfg, include_publish=include_publish)
                if cfg is not None
                else last_decisions.get(str(case), "yaml 不可读")
            )
            stuck.append({"case_dir": str(case), "stuck_at": reason})

    return {
        "rounds": rounds,
        "total_cases": len(case_paths),
        "advanced_cases": len(advanced),
        "rounds_completed": len(rounds),
        "stuck_cases": stuck,
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def format_advance_summary(report: dict) -> str:
    """Human-readable digest of :func:`run_pool_auto_advance` output."""
    rounds = list(report.get("rounds", []))
    lines: list[str] = []
    lines.append(
        f"auto-advance summary: total_cases={report.get('total_cases', 0)} "
        f"advanced={report.get('advanced_cases', 0)} "
        f"rounds={report.get('rounds_completed', 0)}"
    )
    for rr in rounds:
        groups = rr.get("groups", {}) or {}
        if not groups:
            lines.append(f"  round {rr.get('round', '?')}: no work")
            continue
        parts = [
            f"{mode}={(grp.get('total', 0) if isinstance(grp, dict) else 0)}"
            for mode, grp in groups.items()
        ]
        lines.append(f"  round {rr.get('round', '?')}: " + ", ".join(parts))
    stuck = list(report.get("stuck_cases", []))
    if stuck:
        lines.append(f"stuck ({len(stuck)}):")
        for s in stuck:
            lines.append(f"  - {s.get('case_dir', '?')}: {s.get('stuck_at', '?')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Argparse plumbing
# --------------------------------------------------------------------------- #
def register_advance_args(parser: argparse.ArgumentParser) -> None:
    """Register the ``--pool-auto-advance*`` flags onto ``parser``.

    The flags are intentionally additive to the existing ``--pool-*`` group;
    callers should call this *after* ``register_pool_args`` so ``--help``
    output groups them sensibly.
    """
    parser.add_argument(
        "--pool-auto-advance",
        action="store_true",
        default=False,
        help=(
            "Walk every selected case forward by one readiness step per "
            "round, picking discover/data-probe/probe per-case. Off by default."
        ),
    )
    parser.add_argument(
        "--pool-auto-advance-max-rounds",
        type=int,
        default=3,
        help=(
            "Maximum auto-advance rounds before giving up. "
            "Default: 3 (discover -> data-probe -> probe)."
        ),
    )
    parser.add_argument(
        "--pool-auto-advance-include-publish",
        action="store_true",
        default=False,
        help=(
            "If set, ready cases will dispatch a 'publish' round. "
            "Off by default: pool publishing is unsafe and refused by the "
            "underlying pool runner regardless. Mainly intended for tests."
        ),
    )


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _self_test() -> int:  # pragma: no cover - executed only via __main__
    """Tiny in-process smoke test using a tempdir + 3 fake yaml files."""
    import tempfile

    failures: list[str] = []

    if yaml is None:
        print("pyyaml not available; skipping self-test")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Case A: empty execution_state -> "discover"
        case_a = root / "HSP-001-2026-05-01-a"
        case_a.mkdir()
        (case_a / "02-pipeline-gate.yaml").write_text(
            "decision:\n  final_status: draft\n"
            "gate_3_repo_routing:\n  repo_strategy: undecided\n"
            "execution_state:\n"
            "  discovery: {last_run_at: ''}\n"
            "  data_probe: {status: ''}\n"
            "  probe: {last_run_at: ''}\n"
            "  publish_readiness: {status: not_started}\n",
            encoding="utf-8",
        )
        cfg_a = _load_gate(case_a) or {}
        if next_mode_for(cfg_a) != "discover":
            failures.append(f"A: expected 'discover', got {next_mode_for(cfg_a)!r}")

        # Case B: discover done, repo_strategy decided -> "data-probe"
        case_b = root / "HSP-002-2026-05-01-b"
        case_b.mkdir()
        (case_b / "02-pipeline-gate.yaml").write_text(
            "decision:\n  final_status: draft\n"
            "gate_3_repo_routing:\n  repo_strategy: fork_existing\n"
            "execution_state:\n"
            "  discovery: {last_run_at: '2026-05-01T00:00:00Z'}\n"
            "  data_probe: {status: ''}\n"
            "  probe: {last_run_at: ''}\n"
            "  publish_readiness: {status: in_progress}\n",
            encoding="utf-8",
        )
        cfg_b = _load_gate(case_b) or {}
        if next_mode_for(cfg_b) != "data-probe":
            failures.append(f"B: expected 'data-probe', got {next_mode_for(cfg_b)!r}")

        # Case C: data-probe passed -> "probe"
        case_c = root / "HSP-003-2026-05-01-c"
        case_c.mkdir()
        (case_c / "02-pipeline-gate.yaml").write_text(
            "decision:\n  final_status: draft\n"
            "gate_3_repo_routing:\n  repo_strategy: new_repo\n"
            "execution_state:\n"
            "  discovery: {last_run_at: '2026-05-01T00:00:00Z'}\n"
            "  data_probe: {status: passed}\n"
            "  probe: {last_run_at: ''}\n"
            "  publish_readiness: {status: in_progress}\n",
            encoding="utf-8",
        )
        cfg_c = _load_gate(case_c) or {}
        if next_mode_for(cfg_c) != "probe":
            failures.append(f"C: expected 'probe', got {next_mode_for(cfg_c)!r}")

        # describe_advance_decision should return non-empty Chinese
        for cfg in (cfg_a, cfg_b, cfg_c):
            desc = describe_advance_decision(cfg)
            if not desc or not isinstance(desc, str):
                failures.append(f"describe_advance_decision returned {desc!r}")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("pool_advancer self-test: OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(level=logging.INFO)
    sys.exit(_self_test())
