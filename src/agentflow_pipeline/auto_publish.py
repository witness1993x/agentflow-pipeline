"""Safe auto-publish path for the Hotspot-To-GitHub pipeline.

This module implements a fail-closed automation layer on top of the existing
``run_pipeline.py --mode publish`` flow. It is designed to be imported by
``run_pipeline.py`` without requiring any modification to the existing publish
implementation: the heavy lifting (``gh repo create``, git push, writeback,
etc.) is done by a *callable* that the integrating ``main()`` injects.

Why a separate module?
----------------------
``run_pipeline.evaluate_publish_readiness`` already classifies a case as
``ready / blocked_data_probe / blocked_buildability / in_progress / not_started
/ published``. PROGRESS.md flags a remaining gap: there is no automated path
that **honours** this readiness classification while still keeping the human
in the loop. ``--mode publish --execute --allow-publish`` happily fires even
when the readiness is ``blocked_*``; this module refuses to.

Integration patch (apply to ``run_pipeline.py``)
------------------------------------------------
1. **Import (top of file, near other local imports):**

   .. code-block:: python

       from auto_publish import (
           register_auto_publish_args,
           run_auto_publish,
       )

2. **argparse registration (inside ``parse_args`` after the existing
   ``--allow-publish`` flag, before ``return parser.parse_args()``):**

   .. code-block:: python

       register_auto_publish_args(parser)

3. **main() dispatch (insert *before* the existing ``if args.mode == ...``
   branches, immediately after ``print_plan(...)``):**

   .. code-block:: python

       if args.auto_publish or args.auto_publish_dry_run:
           def _publish_callable(_args, _config, _gate_file, _case_dir,
                                 _pool_file, _workspace_root):
               # Reuse the existing --mode publish --execute --allow-publish
               # path verbatim by mutating args and falling through to main's
               # publish branch. Easiest is to call a small helper that wraps
               # what main() already does for mode=="publish".
               _args.mode = "publish"
               _args.execute = True
               _args.allow_publish = True
               return _run_publish_branch(_args, _config, _gate_file,
                                          _case_dir, _pool_file,
                                          _workspace_root)
           return run_auto_publish(
               args, config, gate_file, case_dir, pool_file, workspace_root,
               publish_workflow_callable=_publish_callable,
           )

   The recommended ``_run_publish_branch`` helper is just the body of the
   current publish branch (workspace prep -> probe -> publish_workspace ->
   writeback_probe) extracted into a function so the auto path and the manual
   ``--mode publish`` path share one implementation. ``_publish_callable``
   must return an int exit code.
"""

from __future__ import annotations

import argparse
from typing import Any, Callable, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_get(config: Dict[str, Any], *path: str) -> Any:
    """Walk ``config`` along ``path``; return ``None`` on any miss.

    Fail-closed: callers treat ``None``/empty as "unknown" and refuse publish.
    """
    cursor: Any = config
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
        if cursor is None:
            return None
    return cursor


def _planned_repo_ref(config: Dict[str, Any]) -> str:
    owner = _safe_get(config, "repo_plan", "github_owner") or ""
    name = _safe_get(config, "repo_plan", "repo_name") or ""
    if owner and name:
        return f"{owner}/{name}"
    return ""


def _planned_strategy(config: Dict[str, Any]) -> str:
    return _safe_get(config, "gate_3_repo_routing", "repo_strategy") or "undecided"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_auto_publish_safety(config: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Return ``(ok, blockers)`` describing whether auto-publish is safe.

    The check is fail-closed: any missing / undecided / failing field becomes
    a blocker. ``blockers`` is in Chinese so it surfaces directly to operators
    via the CLI.
    """
    blockers: List[str] = []

    readiness_status = _safe_get(config, "execution_state", "publish_readiness", "status")
    if readiness_status != "ready":
        blockers.append(
            f"publish_readiness.status = {readiness_status!r}，必须为 'ready' 才允许自动 publish。"
        )

    publish_status = _safe_get(config, "execution_state", "publish", "publish_status")
    if publish_status == "passed":
        blockers.append("execution_state.publish.publish_status 已为 'passed'，拒绝重复 publish（幂等保护）。")

    hotspot_id = _safe_get(config, "meta", "hotspot_id")
    if not hotspot_id:
        blockers.append("meta.hotspot_id 为空，无法识别要发布的 hotspot。")

    hotspot_name = _safe_get(config, "meta", "hotspot_name")
    if not hotspot_name:
        blockers.append("meta.hotspot_name 为空，无法生成仓库名称。")

    repo_strategy = _safe_get(config, "gate_3_repo_routing", "repo_strategy")
    if not repo_strategy or repo_strategy == "undecided":
        blockers.append(
            f"gate_3_repo_routing.repo_strategy = {repo_strategy!r}，仍处于未决状态，不允许自动 publish。"
        )

    github_owner = _safe_get(config, "repo_plan", "github_owner")
    if not github_owner:
        blockers.append("repo_plan.github_owner 为空，避免发布到错误账户，已拒绝。")

    repo_name = _safe_get(config, "repo_plan", "repo_name")
    if not repo_name:
        blockers.append("repo_plan.repo_name 为空，避免使用默认/猜测名，已拒绝。")

    veto = _safe_get(config, "decision", "veto_from_gate")
    # Treat any non-empty / truthy veto as a blocker.
    if veto:
        blockers.append(f"decision.veto_from_gate = {veto!r}，存在 gate 否决，禁止自动 publish。")

    kill_signals = _safe_get(config, "gate_4_buildability", "kill_signals_triggered")
    if kill_signals:
        # kill_signals is expected to be a list; any non-empty content blocks.
        blockers.append(
            f"gate_4_buildability.kill_signals_triggered 非空: {kill_signals!r}，禁止自动 publish。"
        )

    fit_verdict = _safe_get(config, "pre_build_analysis", "chainstream_fit", "verdict")
    if fit_verdict != "pass":
        blockers.append(
            f"pre_build_analysis.chainstream_fit.verdict = {fit_verdict!r}，必须为 'pass' 才允许自动 publish。"
        )

    return (len(blockers) == 0), blockers


def auto_publish_dry_run(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a structured preview of what auto-publish *would* do."""
    ok, blockers = check_auto_publish_safety(config)
    readiness = _safe_get(config, "execution_state", "publish_readiness", "status") or "unknown"
    return {
        "would_publish": ok,
        "readiness": readiness,
        "blockers": list(blockers),
        "planned_repo_ref": _planned_repo_ref(config),
        "planned_strategy": _planned_strategy(config),
    }


def register_auto_publish_args(parser: argparse.ArgumentParser) -> None:
    """Register CLI flags for the auto-publish path on ``parser``."""
    parser.add_argument(
        "--auto-publish",
        action="store_true",
        default=False,
        help=(
            "Evaluate publish_readiness and, if 'ready' and all safety gates "
            "pass, run the publish workflow. Requires --auto-publish-confirm "
            "to actually publish."
        ),
    )
    parser.add_argument(
        "--auto-publish-confirm",
        action="store_true",
        default=False,
        help="Second-stage confirmation flag; only with this set will --auto-publish actually publish.",
    )
    parser.add_argument(
        "--auto-publish-dry-run",
        action="store_true",
        default=False,
        help="Print the auto-publish safety check result without executing anything.",
    )


def run_auto_publish(
    args: argparse.Namespace,
    config: Dict[str, Any],
    gate_file: Any,
    case_dir: Any,
    pool_file: Any,
    workspace_root: Any,
    *,
    publish_workflow_callable: Callable[
        [argparse.Namespace, Dict[str, Any], Any, Any, Any, Any],
        int,
    ],
) -> int:
    """Drive the auto-publish dispatch.

    Parameters
    ----------
    args, config, gate_file, case_dir, pool_file, workspace_root:
        Forwarded from ``run_pipeline.main``.
    publish_workflow_callable:
        A callable with signature
        ``(args, config, gate_file, case_dir, pool_file, workspace_root) -> int``
        that performs the real publish (recommended: a function that wraps the
        existing ``--mode publish --execute --allow-publish`` branch). Injected
        rather than imported so tests can stub it out without touching gh/git.

    Returns the process exit code.
    """
    # 1. Dry-run path takes priority over everything else.
    if getattr(args, "auto_publish_dry_run", False):
        result = auto_publish_dry_run(config)
        print("[auto-publish dry-run]")
        print(f"  readiness:        {result['readiness']}")
        print(f"  would_publish:    {result['would_publish']}")
        print(f"  planned_strategy: {result['planned_strategy']}")
        print(f"  planned_repo_ref: {result['planned_repo_ref'] or '<unset>'}")
        if result["blockers"]:
            print("  blockers:")
            for item in result["blockers"]:
                print(f"    - {item}")
        else:
            print("  blockers:         (none)")
        return 0

    # 2. Real auto-publish path: must run safety check.
    ok, blockers = check_auto_publish_safety(config)
    if not ok:
        print("[auto-publish] Safety check FAILED; refusing to publish.")
        for item in blockers:
            print(f"  - {item}")
        return 1

    # 3. Safety passed but explicit confirm flag is missing.
    if not getattr(args, "auto_publish_confirm", False):
        print(
            "[auto-publish] Safety check passed; pass --auto-publish-confirm "
            "to actually publish."
        )
        preview = auto_publish_dry_run(config)
        print(f"  planned_strategy: {preview['planned_strategy']}")
        print(f"  planned_repo_ref: {preview['planned_repo_ref'] or '<unset>'}")
        return 0

    # 4. All conditions met: delegate to the injected publish workflow.
    print("[auto-publish] Safety check passed and confirmation flag provided; publishing.")
    return publish_workflow_callable(
        args, config, gate_file, case_dir, pool_file, workspace_root,
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _synth_ready_config() -> Dict[str, Any]:
    return {
        "meta": {"hotspot_id": "HSP-001", "hotspot_name": "demo-hotspot"},
        "gate_3_repo_routing": {"repo_strategy": "fork_existing"},
        "repo_plan": {"github_owner": "example-org", "repo_name": "demo-hotspot"},
        "decision": {"veto_from_gate": ""},
        "gate_4_buildability": {"kill_signals_triggered": []},
        "pre_build_analysis": {"chainstream_fit": {"verdict": "pass"}},
        "execution_state": {
            "publish_readiness": {"status": "ready"},
            "publish": {"publish_status": "not_started"},
        },
    }


def _synth_blocked_config() -> Dict[str, Any]:
    cfg = _synth_ready_config()
    cfg["execution_state"]["publish_readiness"]["status"] = "blocked_buildability"
    cfg["gate_4_buildability"]["kill_signals_triggered"] = ["build_timeout"]
    cfg["pre_build_analysis"]["chainstream_fit"]["verdict"] = "hold"
    cfg["gate_3_repo_routing"]["repo_strategy"] = "undecided"
    cfg["repo_plan"]["github_owner"] = ""
    cfg["decision"]["veto_from_gate"] = "gate_4_buildability"
    return cfg


def _synth_published_config() -> Dict[str, Any]:
    cfg = _synth_ready_config()
    cfg["execution_state"]["publish"]["publish_status"] = "passed"
    cfg["execution_state"]["publish_readiness"]["status"] = "published"
    return cfg


def _print_check(label: str, config: Dict[str, Any]) -> None:
    ok, blockers = check_auto_publish_safety(config)
    preview = auto_publish_dry_run(config)
    print(f"--- {label} ---")
    print(f"  safety_ok:        {ok}")
    print(f"  readiness:        {preview['readiness']}")
    print(f"  planned_strategy: {preview['planned_strategy']}")
    print(f"  planned_repo_ref: {preview['planned_repo_ref'] or '<unset>'}")
    if blockers:
        print("  blockers:")
        for item in blockers:
            print(f"    - {item}")
    else:
        print("  blockers:         (none)")
    print()


if __name__ == "__main__":
    _print_check("ready config", _synth_ready_config())
    _print_check("blocked config", _synth_blocked_config())
    _print_check("already-published config", _synth_published_config())
