"""Pool-level parallel runner for the Hotspot To GitHub pipeline.

This module provides a thin orchestration layer that scans the ``cases/`` pool,
selects cases by ``decision.final_status`` and a ``fnmatch`` style name glob,
and fans out per-case ``run_pipeline.py`` invocations across a thread pool.

Why subprocess (and not import + thread)?
-----------------------------------------
``run_pipeline.main()`` parses argv with ``argparse`` and mutates module-level
globals (workspace paths, gate dicts, env-derived defaults). Importing it from
multiple worker threads would alias that state and produce undefined behavior
once two cases ran concurrently. Spinning up a fresh ``python3 run_pipeline.py``
per case gives us OS-level isolation for free, at the cost of process startup
(<200ms here, dominated by network I/O anyway). Because each worker spends most
of its time in ``subprocess.run`` waiting on the child, a ``ThreadPoolExecutor``
is sufficient -- no need for ``ProcessPoolExecutor``.

Integration patch into ``run_pipeline.py``
------------------------------------------
This module deliberately does **not** edit ``run_pipeline.py``. To wire it in,
the caller (or a follow-up patch) should:

1. Relax the mutually exclusive group from ``required=True`` to ``required=False``
   inside ``parse_args()``::

       group = parser.add_mutually_exclusive_group(required=False)  # was True
       group.add_argument("--case-dir", ...)
       group.add_argument("--gate-file", ...)

2. Add ``"pool"`` to the ``--mode`` choices list::

       parser.add_argument(
           "--mode",
           default="inspect",
           choices=["inspect", "discover", "data-probe", "kafka-probe",
                    "probe", "publish", "pool"],
           ...
       )

3. Register pool args at the end of ``parse_args()``::

       from pool_runner import register_pool_args
       register_pool_args(parser)

4. Early-return inside ``main()`` *before* ``load_gate_file`` runs::

       if args.mode == "pool":
           from pool_runner import (
               find_pool_cases, run_pool_parallel,
               pool_args_to_kwargs, format_pool_summary,
           )
           kwargs = pool_args_to_kwargs(args)
           cases = find_pool_cases(
               Path(kwargs["pool_cases_dir"]),
               status_filter=kwargs["status_filter"],
               name_glob=kwargs["name_glob"],
           )
           report = run_pool_parallel(
               cases,
               mode=kwargs["pool_mode"],
               max_workers=kwargs["max_workers"],
               extra_args=kwargs["extra_args"],
               run_pipeline_script=Path(__file__).resolve(),
               timeout_per_case=kwargs["timeout_per_case"],
               on_complete_callable=lambda r: print(
                   f"[{r['status']}] {r['case_dir']} ({r['duration_seconds']:.1f}s)"
               ),
           )
           print(format_pool_summary(report))
           return 0 if (report["failed"] == 0 and report["timeout"] == 0) else 1

   And replace the implicit "case-dir or gate-file required" check with an
   explicit one for non-pool modes::

       if args.mode != "pool" and not (args.case_dir or args.gate_file):
           raise PipelineError(
               "--case-dir or --gate-file is required unless --mode pool"
           )
"""
from __future__ import annotations

import argparse
import fnmatch
import logging
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import yaml  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - yaml is a hard dep of the pipeline
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Modes that may safely run in parallel across many cases.
ALLOWED_POOL_MODES: tuple[str, ...] = (
    "inspect",
    "discover",
    "data-probe",
    "kafka-probe",
    "probe",
)
# Modes that are explicitly forbidden no matter what.
FORBIDDEN_POOL_MODES: tuple[str, ...] = ("publish",)


# --------------------------------------------------------------------------- #
# Case discovery
# --------------------------------------------------------------------------- #
def _read_final_status(gate_file: Path) -> str:
    """Best-effort read of ``decision.final_status`` from a gate yaml file.

    Returns "" when the file is unreadable or the field is missing -- callers
    treat that as "unknown" and only include such cases when status_filter is
    None.
    """
    if yaml is None:
        return ""
    try:
        with gate_file.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):  # type: ignore[attr-defined]
        return ""
    decision = data.get("decision") if isinstance(data, dict) else None
    if not isinstance(decision, dict):
        return ""
    raw = decision.get("final_status", "")
    return str(raw) if raw is not None else ""


def _hotspot_id_from_dir(case_dir: Path) -> str:
    """Extract a sortable hotspot id from a case directory name.

    Convention: ``HSP-001-2026-05-01-slug`` -> ``HSP-001``. We also fall back
    to reading the gate yaml's ``meta.hotspot_id`` when the directory name
    doesn't match. Worst case we sort by full directory name.
    """
    name = case_dir.name
    parts = name.split("-")
    if len(parts) >= 2 and parts[0] == "HSP" and parts[1].isdigit():
        return f"HSP-{parts[1]}"
    if yaml is not None:
        gate = case_dir / "02-pipeline-gate.yaml"
        if gate.exists():
            try:
                with gate.open("r", encoding="utf-8") as handle:
                    data = yaml.safe_load(handle) or {}
                meta = data.get("meta") if isinstance(data, dict) else None
                if isinstance(meta, dict):
                    hid = meta.get("hotspot_id", "")
                    if isinstance(hid, str) and hid:
                        return hid
            except (OSError, yaml.YAMLError):  # type: ignore[attr-defined]
                pass
    return name


def find_pool_cases(
    cases_dir: Path,
    *,
    status_filter: list[str] | None = None,
    name_glob: str = "",
) -> list[Path]:
    """Scan ``cases_dir`` and return matching case directories.

    A directory is considered a "case" iff it contains ``02-pipeline-gate.yaml``.

    Parameters
    ----------
    cases_dir:
        Root directory holding case subdirectories. Must exist; if it does not,
        an empty list is returned (caller can decide whether that is fatal).
    status_filter:
        Iterable of acceptable ``decision.final_status`` values
        (e.g. ``["draft", "watch"]``). ``None`` disables filtering.
    name_glob:
        Optional ``fnmatch``-style pattern matched against the case directory
        basename. Empty string disables glob filtering.
    """
    cases_dir = Path(cases_dir).expanduser()
    if not cases_dir.exists() or not cases_dir.is_dir():
        return []

    accepted: list[Path] = []
    for child in cases_dir.iterdir():
        if not child.is_dir():
            continue
        gate = child / "02-pipeline-gate.yaml"
        if not gate.exists():
            continue
        if name_glob and not fnmatch.fnmatch(child.name, name_glob):
            continue
        if status_filter is not None:
            status = _read_final_status(gate)
            if status not in status_filter:
                continue
        accepted.append(child)

    accepted.sort(key=lambda p: (_hotspot_id_from_dir(p), p.name))
    return accepted


# --------------------------------------------------------------------------- #
# Subprocess execution
# --------------------------------------------------------------------------- #
def _tail(text: str, n_lines: int) -> str:
    """Return the last ``n_lines`` of ``text`` (no leading newline)."""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:])


def run_case_subprocess(
    case_dir: Path,
    mode: str,
    extra_args: list[str],
    *,
    python_bin: str = "python3",
    run_pipeline_script: Path,
    timeout: int = 300,
) -> dict:
    """Run a single case via ``python3 run_pipeline.py --case-dir ... --mode ...``.

    Always returns a dict; never raises (timeout and unexpected errors are
    captured and surfaced via the ``status`` field).
    """
    cmd: list[str] = [
        python_bin,
        str(run_pipeline_script),
        "--case-dir",
        str(case_dir),
        "--mode",
        mode,
    ]
    if extra_args:
        cmd.extend(extra_args)

    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - command is fully constructed above
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        stdout = exc.stdout if isinstance(exc.stdout, str) else (
            exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
        )
        stderr = exc.stderr if isinstance(exc.stderr, str) else (
            exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        )
        return {
            "case_dir": str(case_dir),
            "mode": mode,
            "returncode": -1,
            "stdout_tail": _tail(stdout, 50),
            "stderr_tail": _tail(stderr, 20),
            "duration_seconds": round(duration, 3),
            "status": "timeout",
        }
    except (OSError, ValueError) as exc:  # e.g. python_bin missing
        duration = time.monotonic() - started
        return {
            "case_dir": str(case_dir),
            "mode": mode,
            "returncode": -2,
            "stdout_tail": "",
            "stderr_tail": f"subprocess launch failed: {exc!r}",
            "duration_seconds": round(duration, 3),
            "status": "failed",
        }

    duration = time.monotonic() - started
    status = "passed" if proc.returncode == 0 else "failed"
    return {
        "case_dir": str(case_dir),
        "mode": mode,
        "returncode": int(proc.returncode),
        "stdout_tail": _tail(proc.stdout or "", 50),
        "stderr_tail": _tail(proc.stderr or "", 20),
        "duration_seconds": round(duration, 3),
        "status": status,
    }


# --------------------------------------------------------------------------- #
# Parallel orchestration
# --------------------------------------------------------------------------- #
def run_pool_parallel(
    cases: list[Path],
    mode: str,
    *,
    max_workers: int = 4,
    extra_args: list[str] | None = None,
    run_pipeline_script: Path,
    timeout_per_case: int = 300,
    on_complete_callable: Callable[[dict], None] | None = None,
) -> dict:
    """Fan ``cases`` out across a thread pool, one subprocess per case.

    Refuses ``mode == "publish"`` outright (raise ``ValueError``) to prevent
    accidental mass-publishing. ``probe`` is allowed but logged at WARNING
    because it executes shell commands defined in the gate file.
    """
    if mode in FORBIDDEN_POOL_MODES:
        raise ValueError(
            f"mode={mode!r} is not allowed in pool runner "
            "(parallel publish is unsafe)"
        )
    if mode not in ALLOWED_POOL_MODES:
        raise ValueError(
            f"mode={mode!r} is not a recognized pool mode; "
            f"allowed: {', '.join(ALLOWED_POOL_MODES)}"
        )
    if mode == "probe":
        logger.warning(
            "pool runner invoked with mode=probe; executing build commands "
            "across %d case(s) in parallel", len(cases),
        )

    extras = list(extra_args or [])
    results: list[dict] = []
    started = time.monotonic()

    if not cases:
        return {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "timeout": 0,
            "duration_seconds": 0.0,
            "results": [],
        }

    workers = max(1, int(max_workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_case = {
            pool.submit(
                run_case_subprocess,
                case,
                mode,
                extras,
                run_pipeline_script=run_pipeline_script,
                timeout=timeout_per_case,
            ): case
            for case in cases
        }
        for future in as_completed(future_to_case):
            case = future_to_case[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - pool boundary
                result = {
                    "case_dir": str(case),
                    "mode": mode,
                    "returncode": -3,
                    "stdout_tail": "",
                    "stderr_tail": f"executor exception: {exc!r}",
                    "duration_seconds": 0.0,
                    "status": "failed",
                }
            results.append(result)
            if on_complete_callable is not None:
                try:
                    on_complete_callable(result)
                except Exception:  # noqa: BLE001 - callback isolation
                    logger.exception("on_complete_callable raised; ignoring")

    # Stable ordering for downstream consumers / snapshot tests.
    results.sort(key=lambda r: r["case_dir"])

    passed = sum(1 for r in results if r["status"] == "passed")
    failed = sum(1 for r in results if r["status"] == "failed")
    timed_out = sum(1 for r in results if r["status"] == "timeout")

    return {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "timeout": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "results": results,
    }


# --------------------------------------------------------------------------- #
# Argparse plumbing
# --------------------------------------------------------------------------- #
def register_pool_args(parser: argparse.ArgumentParser) -> None:
    """Add ``--pool-*`` arguments to an existing argparse parser."""
    parser.add_argument(
        "--pool-cases-dir",
        default="cases",
        help="Directory holding case subdirectories. Default: cases",
    )
    parser.add_argument(
        "--pool-mode",
        default="inspect",
        choices=list(ALLOWED_POOL_MODES),
        help=(
            "Per-case mode to invoke under the pool. "
            "publish is intentionally not selectable. "
            f"Default: inspect"
        ),
    )
    parser.add_argument(
        "--pool-status-filter",
        default="",
        help=(
            "Comma-separated decision.final_status values to include "
            "(e.g. 'draft,watch'). Empty string -> include all."
        ),
    )
    parser.add_argument(
        "--pool-name-glob",
        default="",
        help="Optional fnmatch-style glob applied to case directory names.",
    )
    parser.add_argument(
        "--pool-max-workers",
        type=int,
        default=4,
        help="Concurrent worker threads (each spawns one subprocess). Default: 4",
    )
    parser.add_argument(
        "--pool-timeout-per-case",
        type=int,
        default=300,
        help="Per-case subprocess timeout in seconds. Default: 300",
    )
    parser.add_argument(
        "--pool-extra-args",
        default="",
        help=(
            "Extra CLI args forwarded to each per-case run_pipeline invocation. "
            "Tokenized with shlex.split."
        ),
    )
    parser.add_argument(
        "--pool-execute",
        action="store_true",
        default=False,
        help=(
            "If set, append --execute to each per-case invocation "
            "(enables real writeback). Off by default for safety."
        ),
    )


def pool_args_to_kwargs(args: argparse.Namespace) -> dict:
    """Convert a parsed ``argparse.Namespace`` into kwargs ready for the runner.

    Mirrors the ``extra_sources_arg_helpers`` style used elsewhere in the repo.
    """
    raw_status = getattr(args, "pool_status_filter", "") or ""
    status_list = [s.strip() for s in raw_status.split(",") if s.strip()]
    status_filter: list[str] | None = status_list if status_list else None

    raw_extra = getattr(args, "pool_extra_args", "") or ""
    extra_tokens = shlex.split(raw_extra) if raw_extra else []
    if getattr(args, "pool_execute", False) and "--execute" not in extra_tokens:
        extra_tokens.append("--execute")

    return {
        "pool_cases_dir": getattr(args, "pool_cases_dir", "cases") or "cases",
        "pool_mode": getattr(args, "pool_mode", "inspect") or "inspect",
        "status_filter": status_filter,
        "name_glob": getattr(args, "pool_name_glob", "") or "",
        "max_workers": int(getattr(args, "pool_max_workers", 4) or 4),
        "timeout_per_case": int(getattr(args, "pool_timeout_per_case", 300) or 300),
        "extra_args": extra_tokens,
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def format_pool_summary(report: dict) -> str:
    """Return a compact, human-readable digest of a ``run_pool_parallel`` result."""
    total = int(report.get("total", 0))
    passed = int(report.get("passed", 0))
    failed = int(report.get("failed", 0))
    timed_out = int(report.get("timeout", 0))
    duration = float(report.get("duration_seconds", 0.0))
    results: list[dict] = list(report.get("results", []))

    lines: list[str] = []
    lines.append(
        f"pool summary: total={total} passed={passed} "
        f"failed={failed} timeout={timed_out} "
        f"wall={duration:.2f}s"
    )

    slowest = sorted(
        results,
        key=lambda r: float(r.get("duration_seconds", 0.0) or 0.0),
        reverse=True,
    )[:3]
    if slowest:
        lines.append("slowest:")
        for r in slowest:
            lines.append(
                f"  - {r.get('case_dir', '?')} "
                f"[{r.get('status', '?')}] "
                f"{float(r.get('duration_seconds', 0.0) or 0.0):.2f}s"
            )

    failures = [
        r for r in results
        if r.get("status") in {"failed", "timeout"}
    ]
    if failures:
        lines.append("failures:")
        for r in failures:
            lines.append(
                f"  - {r.get('case_dir', '?')} "
                f"[{r.get('status', '?')}] rc={r.get('returncode', '?')}"
            )

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _self_test() -> int:  # pragma: no cover - executed only via __main__
    """Run a tiny in-process smoke test using a fake subprocess wrapper."""
    import tempfile

    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cases_root = root / "cases"
        cases_root.mkdir()
        for i in range(3):
            d = cases_root / f"HSP-{i + 1:03d}-2026-05-01-demo{i}"
            d.mkdir()
            (d / "02-pipeline-gate.yaml").write_text(
                "decision:\n  final_status: watch\n",
                encoding="utf-8",
            )

        cases = find_pool_cases(cases_root, status_filter=["watch"])
        if len(cases) != 3:
            failures.append(f"expected 3 cases, got {len(cases)}")
        if [p.name for p in cases] != sorted(p.name for p in cases):
            failures.append("cases not sorted")

        # Monkeypatch run_case_subprocess via direct call into a fake.
        def _fake(case_dir: Path, mode: str, extras: list[str], **_: Any) -> dict:
            return {
                "case_dir": str(case_dir),
                "mode": mode,
                "returncode": 0,
                "stdout_tail": "ok",
                "stderr_tail": "",
                "duration_seconds": 0.01,
                "status": "passed",
            }

        global run_case_subprocess  # type: ignore[name-defined]
        original = run_case_subprocess
        run_case_subprocess = _fake  # type: ignore[assignment]
        try:
            report = run_pool_parallel(
                cases,
                mode="inspect",
                max_workers=3,
                run_pipeline_script=Path("/dev/null"),
                timeout_per_case=10,
            )
        finally:
            run_case_subprocess = original  # type: ignore[assignment]

        for key in ("total", "passed", "failed", "timeout", "results"):
            if key not in report:
                failures.append(f"report missing key: {key}")
        if report.get("total") != 3 or report.get("passed") != 3:
            failures.append(f"unexpected counts: {report}")

        summary = format_pool_summary(report)
        if "passed=3" not in summary:
            failures.append(f"summary missing pass count: {summary!r}")

        try:
            run_pool_parallel(
                cases,
                mode="publish",
                run_pipeline_script=Path("/dev/null"),
            )
        except ValueError:
            pass
        else:
            failures.append("publish mode did not raise ValueError")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("pool_runner self-test: OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    sys.exit(_self_test())
