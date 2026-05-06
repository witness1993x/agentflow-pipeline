"""OS-level scheduler installer for ``agentflow-scan``.

This module installs the ``agentflow-scan`` console script as a recurring
job on macOS (via launchd) or linux (via systemd user timers). The default
schedule is twice a day (09:00 and 21:00 local time).

Design notes
------------
* **Fail-closed by default.** All install / uninstall functions accept a
  ``dry_run`` flag and the CLI defaults to ``--dry-run``; the user must
  pass ``--apply`` to actually mutate files or invoke ``launchctl`` /
  ``systemctl``.
* **Pure stdlib.** ``plistlib`` produces the macOS plist; systemd unit
  files are assembled with multi-line f-strings.
* **Subprocess errors are caught.** Any failure during install/uninstall
  is appended to the result's ``errors`` list rather than raising.

The module is independent of ``cli.py`` and ``scan_hotspots.py``; it
exposes its own ``main()`` console entry point under the
``agentflow-schedule`` script name.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleSpec:
    """Declarative schedule specification consumed by all installers.

    Attributes
    ----------
    label:
        launchd label / systemd unit basename, e.g.
        ``"com.agentflow.scan.daily"``.
    times:
        Local-time HH:MM strings the job should fire at. Ignored when
        ``mode == "daemon"``.
    command:
        Full argv list passed to launchd's ``ProgramArguments`` or
        systemd's ``ExecStart``.
    working_dir:
        Resolved directory the job runs from.
    log_dir:
        Directory where stdout/stderr will be appended (one file per
        stream for launchd; systemd inherits journal).
    description:
        Human-readable note inserted into the unit/plist as a comment.
    mode:
        Either ``"cron"`` (default; ``StartCalendarInterval`` /
        ``OnCalendar`` recurring job) or ``"daemon"`` (long-running
        process kept alive by launchd / systemd, e.g. the
        ``agentflow-tg-listen`` Telegram callback listener). In
        ``daemon`` mode ``times`` is ignored and the resulting unit
        has ``RunAtLoad=true`` + ``KeepAlive`` (macOS) or
        ``Type=simple`` + ``Restart=on-failure`` (linux).
    """

    label: str
    times: list[str]
    command: list[str]
    working_dir: Path
    log_dir: Path
    description: str = ""
    mode: str = "cron"


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def detect_platform() -> str:
    """Return ``"macos"``, ``"linux"`` or ``"unknown"`` for the host OS."""
    p = sys.platform
    if p == "darwin":
        return "macos"
    if p.startswith("linux"):
        return "linux"
    return "unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_hhmm(s: str) -> tuple[int, int]:
    """Parse a HH:MM string into ``(hour, minute)`` ints.

    Raises ``ValueError`` on bad input.
    """
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time {s!r} (expected HH:MM)")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"time out of range: {s!r}")
    return h, m


def _ensure_log_dir(spec: ScheduleSpec, *, dry_run: bool, actions: list[str]) -> None:
    """Create the spec's log dir (or pretend to under dry-run)."""
    if dry_run:
        actions.append(f"would mkdir -p {spec.log_dir}")
        return
    spec.log_dir.mkdir(parents=True, exist_ok=True)
    actions.append(f"mkdir -p {spec.log_dir}")


# ---------------------------------------------------------------------------
# macOS plist generation
# ---------------------------------------------------------------------------


def build_macos_plist(spec: ScheduleSpec) -> str:
    """Return a launchd-compatible plist XML string for ``spec``.

    For ``spec.mode == "cron"`` (the default) the plist contains:

    * ``Label`` from ``spec.label``
    * ``ProgramArguments`` from ``spec.command``
    * ``StartCalendarInterval`` as a list of dicts (one per time)
    * ``WorkingDirectory``, ``StandardOutPath``, ``StandardErrorPath``
    * ``RunAtLoad = False`` and ``KeepAlive = False``

    For ``spec.mode == "daemon"`` the plist drops
    ``StartCalendarInterval`` entirely and instead sets:

    * ``RunAtLoad = True`` (start the daemon on user-login / load)
    * ``KeepAlive`` as a dict ``{"SuccessfulExit": False,
      "Crashed": True, "NetworkState": True}`` so launchd auto-
      restarts on crash and waits for network, but a clean ``exit 0``
      is honoured (lets the operator stop the daemon gracefully)
    * ``ThrottleInterval = 10`` (seconds) to prevent rapid restart
      loops if the daemon dies immediately on launch
    """
    stdout_path = str(spec.log_dir / f"{spec.label}.out.log")
    stderr_path = str(spec.log_dir / f"{spec.label}.err.log")

    if spec.mode == "daemon":
        payload: dict[str, Any] = {
            "Label": spec.label,
            "ProgramArguments": list(spec.command),
            "WorkingDirectory": str(spec.working_dir),
            "StandardOutPath": stdout_path,
            "StandardErrorPath": stderr_path,
            "RunAtLoad": True,
            "KeepAlive": {
                "SuccessfulExit": False,
                "Crashed": True,
                "NetworkState": True,
            },
            "ThrottleInterval": 10,
        }
    else:
        intervals: list[dict[str, int]] = []
        for t in spec.times:
            h, m = _parse_hhmm(t)
            intervals.append({"Hour": h, "Minute": m})

        payload = {
            "Label": spec.label,
            "ProgramArguments": list(spec.command),
            "WorkingDirectory": str(spec.working_dir),
            "StandardOutPath": stdout_path,
            "StandardErrorPath": stderr_path,
            "RunAtLoad": False,
            "KeepAlive": False,
            "StartCalendarInterval": intervals,
        }

    if spec.description:
        # launchd has no first-class description; stash in a custom key.
        payload["Comment"] = spec.description

    raw = plistlib.dumps(payload, sort_keys=False)
    return raw.decode("utf-8")


def _macos_plist_path(label: str) -> Path:
    """Return the LaunchAgent plist install path for ``label``."""
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _macos_uid() -> int:
    """Return current user's uid (used by ``launchctl bootstrap gui/<uid>``)."""
    try:
        return os.getuid()  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - Windows path
        return 0


def install_macos_launchd(
    spec: ScheduleSpec,
    *,
    dry_run: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Install ``spec`` as a launchd LaunchAgent.

    Parameters
    ----------
    dry_run:
        When ``True`` (default), no files are written and no
        ``launchctl`` calls are made. The would-be plist content and
        commands are returned in the result.
    force:
        Required to overwrite an existing plist at the install path.
    """
    actions: list[str] = []
    errors: list[str] = []
    plist_path = _macos_plist_path(spec.label)
    plist_text = build_macos_plist(spec)

    result: dict[str, Any] = {
        "platform": "macos",
        "label": spec.label,
        "plist_path": str(plist_path),
        "plist_text": plist_text,
        "actions": actions,
        "errors": errors,
        "status": "unknown",
    }

    _ensure_log_dir(spec, dry_run=dry_run, actions=actions)

    if dry_run:
        actions.append(f"would write plist to {plist_path}")
        actions.append(
            f"would run: launchctl bootstrap gui/{_macos_uid()} {plist_path}"
        )
        result["status"] = "dry_run"
        return result

    # Real install path.
    if plist_path.exists() and not force:
        result["status"] = "exists"
        actions.append(f"plist already exists at {plist_path}; pass force=True to overwrite")
        return result

    try:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist_text, encoding="utf-8")
        actions.append(f"wrote plist {plist_path}")
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"write plist failed: {exc!r}")
        result["status"] = "failed"
        return result

    # bootout existing (best-effort) then bootstrap.
    bootout_cmd = ["launchctl", "bootout", f"gui/{_macos_uid()}", str(plist_path)]
    bootstrap_cmd = ["launchctl", "bootstrap", f"gui/{_macos_uid()}", str(plist_path)]

    try:
        subprocess.run(bootout_cmd, capture_output=True, text=True, check=False)
        actions.append(" ".join(bootout_cmd))
    except Exception as exc:
        errors.append(f"bootout failed (non-fatal): {exc!r}")

    try:
        cp = subprocess.run(bootstrap_cmd, capture_output=True, text=True, check=False)
        actions.append(" ".join(bootstrap_cmd))
        if cp.returncode != 0:
            errors.append(
                f"bootstrap rc={cp.returncode} stderr={cp.stderr.strip()!r}"
            )
            result["status"] = "failed"
            return result
    except Exception as exc:
        errors.append(f"bootstrap failed: {exc!r}")
        result["status"] = "failed"
        return result

    result["status"] = "installed"
    return result


def uninstall_macos_launchd(label: str, *, dry_run: bool = True) -> dict[str, Any]:
    """Uninstall the LaunchAgent identified by ``label``."""
    actions: list[str] = []
    errors: list[str] = []
    plist_path = _macos_plist_path(label)

    result: dict[str, Any] = {
        "platform": "macos",
        "label": label,
        "plist_path": str(plist_path),
        "actions": actions,
        "errors": errors,
        "status": "unknown",
    }

    bootout_cmd = ["launchctl", "bootout", f"gui/{_macos_uid()}", str(plist_path)]

    if dry_run:
        actions.append(f"would run: {' '.join(bootout_cmd)}")
        actions.append(f"would unlink {plist_path}")
        result["status"] = "dry_run"
        return result

    try:
        subprocess.run(bootout_cmd, capture_output=True, text=True, check=False)
        actions.append(" ".join(bootout_cmd))
    except Exception as exc:
        errors.append(f"bootout failed (non-fatal): {exc!r}")

    if plist_path.exists():
        try:
            plist_path.unlink()
            actions.append(f"unlinked {plist_path}")
        except Exception as exc:
            errors.append(f"unlink failed: {exc!r}")
            result["status"] = "failed"
            return result
    else:
        actions.append(f"plist not found at {plist_path}")

    result["status"] = "uninstalled"
    return result


def status_macos(label: str) -> dict[str, Any]:
    """Return current launchd status for ``label`` (best-effort)."""
    actions: list[str] = []
    errors: list[str] = []
    plist_path = _macos_plist_path(label)
    result: dict[str, Any] = {
        "platform": "macos",
        "label": label,
        "plist_path": str(plist_path),
        "plist_exists": plist_path.exists(),
        "loaded": False,
        "raw": "",
        "actions": actions,
        "errors": errors,
    }

    cmd = ["launchctl", "list"]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
        actions.append(" ".join(cmd))
        result["raw"] = cp.stdout
        for line in cp.stdout.splitlines():
            if label in line:
                result["loaded"] = True
                result["raw_line"] = line
                break
    except Exception as exc:
        errors.append(f"launchctl list failed: {exc!r}")
    return result


# ---------------------------------------------------------------------------
# linux systemd unit generation
# ---------------------------------------------------------------------------


def _shell_quote(arg: str) -> str:
    """Return ``arg`` quoted for safe inclusion in a systemd ExecStart line."""
    if not arg:
        return '""'
    safe = all(c.isalnum() or c in "/._-=:," for c in arg)
    if safe:
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_systemd_units(spec: ScheduleSpec) -> tuple[str, str]:
    """Return ``(service_unit_text, timer_unit_text)`` for ``spec``.

    For ``spec.mode == "cron"`` (default) the service is
    ``Type=oneshot`` and writes stdout/stderr to files under
    ``spec.log_dir``. The timer fires once per HH:MM in ``spec.times``
    using ``OnCalendar=*-*-* HH:MM:00``.

    For ``spec.mode == "daemon"`` the service is ``Type=simple`` with
    ``Restart=on-failure`` + ``RestartSec=10`` so systemd keeps the
    long-running process alive across crashes; the returned
    ``timer_unit_text`` is the empty string (no timer is generated).
    """
    description = spec.description or f"agentflow-scan recurring job ({spec.label})"
    exec_start = " ".join(_shell_quote(arg) for arg in spec.command)
    stdout_path = spec.log_dir / f"{spec.label}.out.log"
    stderr_path = spec.log_dir / f"{spec.label}.err.log"

    if spec.mode == "daemon":
        service_text = (
            "[Unit]\n"
            f"Description={description}\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={spec.working_dir}\n"
            f"ExecStart={exec_start}\n"
            f"StandardOutput=append:{stdout_path}\n"
            f"StandardError=append:{stderr_path}\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        # daemon mode has no timer companion
        return service_text, ""

    service_text = (
        "[Unit]\n"
        f"Description={description}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={spec.working_dir}\n"
        f"ExecStart={exec_start}\n"
        f"StandardOutput=append:{stdout_path}\n"
        f"StandardError=append:{stderr_path}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )

    on_calendar_lines = "\n".join(
        f"OnCalendar=*-*-* {_format_hhmm(t)}:00" for t in spec.times
    )
    timer_text = (
        "[Unit]\n"
        f"Description={description} (timer)\n"
        "\n"
        "[Timer]\n"
        f"{on_calendar_lines}\n"
        "Persistent=true\n"
        f"Unit={spec.label}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service_text, timer_text


def _format_hhmm(s: str) -> str:
    """Normalise a HH:MM string with zero-padding."""
    h, m = _parse_hhmm(s)
    return f"{h:02d}:{m:02d}"


def _systemd_unit_dir(*, user_scope: bool) -> Path:
    """Return the directory unit files are installed into."""
    if user_scope:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        return base / "systemd" / "user"
    return Path("/etc/systemd/system")


def _systemctl_cmd(*, user_scope: bool) -> list[str]:
    """Return the base ``systemctl`` argv (with ``--user`` if requested)."""
    return ["systemctl", "--user"] if user_scope else ["systemctl"]


def install_systemd_timer(
    spec: ScheduleSpec,
    *,
    dry_run: bool = True,
    user_scope: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Install a systemd service+timer pair for ``spec``.

    For ``spec.mode == "daemon"`` only the ``.service`` file is
    installed (no timer) and ``systemctl enable --now`` targets the
    service directly.
    """
    actions: list[str] = []
    errors: list[str] = []
    service_text, timer_text = build_systemd_units(spec)
    unit_dir = _systemd_unit_dir(user_scope=user_scope)
    service_path = unit_dir / f"{spec.label}.service"
    timer_path = unit_dir / f"{spec.label}.timer"
    is_daemon = spec.mode == "daemon"

    result: dict[str, Any] = {
        "platform": "linux",
        "label": spec.label,
        "user_scope": user_scope,
        "service_path": str(service_path),
        "timer_path": "" if is_daemon else str(timer_path),
        "service_text": service_text,
        "timer_text": timer_text,
        "actions": actions,
        "errors": errors,
        "status": "unknown",
    }

    _ensure_log_dir(spec, dry_run=dry_run, actions=actions)

    sysctl = _systemctl_cmd(user_scope=user_scope)
    reload_cmd = sysctl + ["daemon-reload"]
    if is_daemon:
        enable_cmd = sysctl + ["enable", "--now", f"{spec.label}.service"]
    else:
        enable_cmd = sysctl + ["enable", "--now", f"{spec.label}.timer"]

    if dry_run:
        actions.append(f"would write {service_path}")
        if not is_daemon:
            actions.append(f"would write {timer_path}")
        actions.append(f"would run: {' '.join(reload_cmd)}")
        actions.append(f"would run: {' '.join(enable_cmd)}")
        result["status"] = "dry_run"
        return result

    existing = service_path.exists() or (not is_daemon and timer_path.exists())
    if existing and not force:
        result["status"] = "exists"
        actions.append(
            f"unit files already exist under {unit_dir}; pass force=True to overwrite"
        )
        return result

    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        service_path.write_text(service_text, encoding="utf-8")
        actions.append(f"wrote {service_path}")
        if not is_daemon:
            timer_path.write_text(timer_text, encoding="utf-8")
            actions.append(f"wrote {timer_path}")
    except Exception as exc:
        errors.append(f"write units failed: {exc!r}")
        result["status"] = "failed"
        return result

    for cmd in (reload_cmd, enable_cmd):
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
            actions.append(" ".join(cmd))
            if cp.returncode != 0:
                errors.append(
                    f"{' '.join(cmd)} rc={cp.returncode} stderr={cp.stderr.strip()!r}"
                )
        except Exception as exc:
            errors.append(f"{' '.join(cmd)} failed: {exc!r}")

    result["status"] = "failed" if errors else "installed"
    return result


def uninstall_systemd_timer(
    label: str,
    *,
    dry_run: bool = True,
    user_scope: bool = True,
) -> dict[str, Any]:
    """Disable and remove the systemd service+timer pair for ``label``."""
    actions: list[str] = []
    errors: list[str] = []
    unit_dir = _systemd_unit_dir(user_scope=user_scope)
    service_path = unit_dir / f"{label}.service"
    timer_path = unit_dir / f"{label}.timer"

    result: dict[str, Any] = {
        "platform": "linux",
        "label": label,
        "user_scope": user_scope,
        "service_path": str(service_path),
        "timer_path": str(timer_path),
        "actions": actions,
        "errors": errors,
        "status": "unknown",
    }

    sysctl = _systemctl_cmd(user_scope=user_scope)
    disable_cmd = sysctl + ["disable", "--now", f"{label}.timer"]
    reload_cmd = sysctl + ["daemon-reload"]

    if dry_run:
        actions.append(f"would run: {' '.join(disable_cmd)}")
        actions.append(f"would unlink {service_path}")
        actions.append(f"would unlink {timer_path}")
        actions.append(f"would run: {' '.join(reload_cmd)}")
        result["status"] = "dry_run"
        return result

    try:
        subprocess.run(disable_cmd, capture_output=True, text=True, check=False)
        actions.append(" ".join(disable_cmd))
    except Exception as exc:
        errors.append(f"disable failed (non-fatal): {exc!r}")

    for p in (service_path, timer_path):
        if p.exists():
            try:
                p.unlink()
                actions.append(f"unlinked {p}")
            except Exception as exc:
                errors.append(f"unlink {p} failed: {exc!r}")

    try:
        subprocess.run(reload_cmd, capture_output=True, text=True, check=False)
        actions.append(" ".join(reload_cmd))
    except Exception as exc:
        errors.append(f"daemon-reload failed (non-fatal): {exc!r}")

    result["status"] = "failed" if errors else "uninstalled"
    return result


def status_systemd(label: str, *, user_scope: bool = True) -> dict[str, Any]:
    """Return systemd timer status for ``label`` (best-effort)."""
    actions: list[str] = []
    errors: list[str] = []
    unit_dir = _systemd_unit_dir(user_scope=user_scope)
    timer_path = unit_dir / f"{label}.timer"

    sysctl = _systemctl_cmd(user_scope=user_scope)
    cmd = sysctl + ["list-timers", "--all"]

    result: dict[str, Any] = {
        "platform": "linux",
        "label": label,
        "user_scope": user_scope,
        "timer_path": str(timer_path),
        "timer_exists": timer_path.exists(),
        "loaded": False,
        "raw": "",
        "actions": actions,
        "errors": errors,
    }

    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
        actions.append(" ".join(cmd))
        result["raw"] = cp.stdout
        for line in cp.stdout.splitlines():
            if label in line:
                result["loaded"] = True
                result["raw_line"] = line
                break
    except Exception as exc:
        errors.append(f"list-timers failed: {exc!r}")
    return result


# ---------------------------------------------------------------------------
# Default spec
# ---------------------------------------------------------------------------


DEFAULT_TIMES: list[str] = ["09:00", "21:00"]


def build_default_scan_spec(
    *,
    root: Path,
    label: str = "com.agentflow.scan.daily",
    times: list[str] | None = None,
    scan_extra_args: list[str] | None = None,
) -> ScheduleSpec:
    """Construct a :class:`ScheduleSpec` for the default ``agentflow-scan`` job.

    Parameters
    ----------
    root:
        Host project root. Becomes ``--root`` for ``agentflow-scan`` and
        the working directory for the job.
    label:
        launchd / systemd label.
    times:
        HH:MM strings. Defaults to ``["09:00", "21:00"]``.
    scan_extra_args:
        Extra argv passed to ``agentflow-scan`` after the ``--root`` flag.
    """
    root = Path(root).expanduser().resolve()
    times = list(times) if times else list(DEFAULT_TIMES)
    extra = list(scan_extra_args) if scan_extra_args else []

    scan_bin = shutil.which("agentflow-scan") or "agentflow-scan"
    command: list[str] = [scan_bin, "--root", str(root), *extra]

    return ScheduleSpec(
        label=label,
        times=times,
        command=command,
        working_dir=root,
        log_dir=root / "trends" / "_logs",
        description=f"agentflow-scan twice-daily run at {','.join(times)}",
    )


def build_default_listener_spec(
    *,
    root: Path,
    label: str = "com.agentflow.tg-listener",
    listener_extra_args: list[str] | None = None,
) -> ScheduleSpec:
    """Construct a daemon :class:`ScheduleSpec` for ``agentflow-tg-listen``.

    The Telegram callback listener is a long-running process that polls
    Telegram's ``getUpdates`` endpoint and dispatches inline-keyboard
    button clicks to the framework's case_actions handlers. It must be
    kept alive across crashes and on user-login, hence ``mode="daemon"``.

    Parameters
    ----------
    root:
        Host project root. Used as both the listener's working
        directory and the parent of the ``trends/_logs/`` directory
        where stdout/stderr are appended.
    label:
        launchd label / systemd unit basename. Default
        ``"com.agentflow.tg-listener"``.
    listener_extra_args:
        Extra argv passed to ``agentflow-tg-listen`` after the
        ``--root`` flag (e.g. ``["--bot-token-env", "TELEGRAM_BOT_TOKEN",
        "--callback-secret-env", "TELEGRAM_CALLBACK_SECRET"]``).
    """
    root = Path(root).expanduser().resolve()
    extra = list(listener_extra_args) if listener_extra_args else []

    listener_bin = shutil.which("agentflow-tg-listen") or "agentflow-tg-listen"
    command: list[str] = [listener_bin, "--root", str(root), *extra]

    return ScheduleSpec(
        label=label,
        times=[],
        command=command,
        working_dir=root,
        log_dir=root / "trends" / "_logs",
        description="agentflow-tg-listen Telegram callback daemon",
        mode="daemon",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _split_csv(value: str | None) -> list[str]:
    """Split a comma-separated string into a list, stripping empties."""
    if not value:
        return []
    return [chunk.strip() for chunk in value.split(",") if chunk.strip()]


def _parse_scan_args(value: str | None) -> list[str]:
    """Split a free-form ``--scan-args`` string by whitespace."""
    if not value:
        return []
    return value.split()


def _build_parser() -> argparse.ArgumentParser:
    """Construct the ``agentflow-schedule`` argparse parser."""
    parser = argparse.ArgumentParser(
        prog="agentflow-schedule",
        description=(
            "Install agentflow-scan as a recurring OS-level job "
            "(macOS launchd or linux systemd timers)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--platform",
            choices=("macos", "linux", "auto"),
            default="auto",
            help="Target OS scheduler. Default: auto (detect from sys.platform).",
        )
        p.add_argument(
            "--label",
            default="com.agentflow.scan.daily",
            help="launchd label / systemd unit basename.",
        )
        scope = p.add_mutually_exclusive_group()
        scope.add_argument(
            "--user",
            dest="user_scope",
            action="store_true",
            help="Linux only: install under ~/.config/systemd/user (default).",
        )
        scope.add_argument(
            "--system",
            dest="user_scope",
            action="store_false",
            help="Linux only: install under /etc/systemd/system (requires root).",
        )
        p.set_defaults(user_scope=True)
        p.add_argument(
            "--apply",
            action="store_true",
            help="Actually perform the install/uninstall (default is dry-run).",
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="Force dry-run mode (default behaviour; opposite of --apply).",
        )

    p_install = sub.add_parser("install", help="Install the recurring job.")
    _add_common(p_install)
    p_install.add_argument(
        "--mode",
        choices=("cron", "daemon"),
        default="cron",
        help=(
            'Job shape. "cron" (default) installs a recurring '
            "agentflow-scan job at --times. \"daemon\" installs a "
            "long-running agentflow-tg-listen process kept alive by "
            "launchd/systemd (RunAtLoad + KeepAlive on macOS, "
            "Type=simple + Restart=on-failure on linux). In daemon "
            "mode --times is ignored."
        ),
    )
    p_install.add_argument(
        "--times",
        default=",".join(DEFAULT_TIMES),
        help='Comma-separated HH:MM times (default: "09:00,21:00"). Ignored in --mode daemon.',
    )
    p_install.add_argument(
        "--root",
        default=str(Path.cwd()),
        help="Host project root passed as --root to the underlying binary.",
    )
    p_install.add_argument(
        "--scan-args",
        default="",
        help=(
            "Extra argv passed to agentflow-scan in cron mode "
            '(e.g. "--sources github,hackernews"). Also accepted as '
            "passthrough args in daemon mode if --daemon-args is empty."
        ),
    )
    p_install.add_argument(
        "--daemon-args",
        default="",
        help=(
            "Extra argv passed to the daemon binary in --mode daemon "
            '(e.g. "--bot-token-env TELEGRAM_BOT_TOKEN '
            '--callback-secret-env TELEGRAM_CALLBACK_SECRET"). '
            "Takes precedence over --scan-args in daemon mode."
        ),
    )
    p_install.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing plist / unit files if present.",
    )

    p_uninstall = sub.add_parser("uninstall", help="Remove the recurring job.")
    _add_common(p_uninstall)

    p_status = sub.add_parser("status", help="Show current scheduler status.")
    p_status.add_argument(
        "--platform",
        choices=("macos", "linux", "auto"),
        default="auto",
    )
    p_status.add_argument("--label", default="com.agentflow.scan.daily")
    p_status.add_argument(
        "--user",
        dest="user_scope",
        action="store_true",
    )
    p_status.add_argument(
        "--system",
        dest="user_scope",
        action="store_false",
    )
    p_status.set_defaults(user_scope=True)

    return parser


def _resolve_platform(arg: str) -> str:
    """Resolve ``--platform`` (``auto`` -> :func:`detect_platform`)."""
    if arg == "auto":
        return detect_platform()
    return arg


def _print_result(result: dict[str, Any]) -> None:
    """Render an install/uninstall result dict to stdout for humans."""
    print(f"[agentflow-schedule] platform = {result.get('platform')}")
    print(f"[agentflow-schedule] label    = {result.get('label')}")
    print(f"[agentflow-schedule] status   = {result.get('status')}")

    if "plist_path" in result:
        print(f"[agentflow-schedule] plist    = {result['plist_path']}")
    if "service_path" in result:
        print(f"[agentflow-schedule] service  = {result['service_path']}")
        if result.get("timer_path"):
            print(f"[agentflow-schedule] timer    = {result['timer_path']}")

    if result.get("plist_text"):
        print("\n----- plist -----")
        print(result["plist_text"])
    if result.get("service_text"):
        print("\n----- {label}.service -----".format(label=result.get("label")))
        print(result["service_text"])
        if result.get("timer_text"):
            print("----- {label}.timer -----".format(label=result.get("label")))
            print(result["timer_text"])

    actions = result.get("actions") or []
    if actions:
        print("\n----- actions -----")
        for a in actions:
            print(f"  - {a}")
    errors = result.get("errors") or []
    if errors:
        print("\n----- errors -----")
        for e in errors:
            print(f"  ! {e}")


def main(argv: list[str] | None = None) -> int:
    """``agentflow-schedule`` console entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    platform = _resolve_platform(args.platform)
    if platform not in ("macos", "linux"):
        print(
            f"[agentflow-schedule] unsupported platform: {platform}; "
            "supply --platform macos|linux explicitly.",
            file=sys.stderr,
        )
        return 2

    # --apply takes precedence; otherwise dry-run is the default.
    apply_flag = bool(getattr(args, "apply", False))
    dry_run = not apply_flag

    if args.cmd == "install":
        mode = getattr(args, "mode", "cron")
        if mode == "daemon":
            # Use --daemon-args if provided; otherwise fall back to --scan-args
            # (treated as generic passthrough in daemon context).
            daemon_args_str = getattr(args, "daemon_args", "") or args.scan_args
            # In daemon mode the default label should reflect that this is
            # the listener; only override the global default though, to
            # respect any explicit --label the user passed.
            label = args.label
            if label == "com.agentflow.scan.daily":
                label = "com.agentflow.tg-listener"
            spec = build_default_listener_spec(
                root=Path(args.root),
                label=label,
                listener_extra_args=_parse_scan_args(daemon_args_str),
            )
        else:
            spec = build_default_scan_spec(
                root=Path(args.root),
                label=args.label,
                times=_split_csv(args.times) or None,
                scan_extra_args=_parse_scan_args(args.scan_args),
            )
        if platform == "macos":
            result = install_macos_launchd(
                spec, dry_run=dry_run, force=bool(args.force)
            )
        else:
            result = install_systemd_timer(
                spec,
                dry_run=dry_run,
                user_scope=bool(args.user_scope),
                force=bool(args.force),
            )
        _print_result(result)
        return 0 if result.get("status") in {"installed", "dry_run", "exists"} else 1

    if args.cmd == "uninstall":
        if platform == "macos":
            result = uninstall_macos_launchd(args.label, dry_run=dry_run)
        else:
            result = uninstall_systemd_timer(
                args.label, dry_run=dry_run, user_scope=bool(args.user_scope)
            )
        _print_result(result)
        return 0 if result.get("status") in {"uninstalled", "dry_run"} else 1

    if args.cmd == "status":
        if platform == "macos":
            result = status_macos(args.label)
        else:
            result = status_systemd(args.label, user_scope=bool(args.user_scope))
        print(f"[agentflow-schedule] platform = {platform}")
        print(f"[agentflow-schedule] label    = {args.label}")
        print(f"[agentflow-schedule] loaded   = {result.get('loaded')}")
        if "plist_exists" in result:
            print(f"[agentflow-schedule] plist    = {result['plist_path']}")
            print(f"[agentflow-schedule] exists   = {result['plist_exists']}")
        if "timer_exists" in result:
            print(f"[agentflow-schedule] timer    = {result['timer_path']}")
            print(f"[agentflow-schedule] exists   = {result['timer_exists']}")
        if result.get("raw_line"):
            print(f"[agentflow-schedule] entry    = {result['raw_line']}")
        return 0

    parser.error(f"unknown command: {args.cmd}")
    return 2  # pragma: no cover - argparse already exits


# ---------------------------------------------------------------------------
# Self-test entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    demo_root = Path("/tmp/host-demo").resolve()
    spec = build_default_scan_spec(root=demo_root)

    print("=== build_default_scan_spec ===")
    print(spec)

    print("\n=== install_macos_launchd(dry_run=True) ===")
    mac_result = install_macos_launchd(spec, dry_run=True)
    _print_result(mac_result)

    print("\n=== install_systemd_timer(dry_run=True) ===")
    linux_result = install_systemd_timer(spec, dry_run=True)
    print("--- service+timer head (30 lines) ---")
    head = (
        linux_result["service_text"] + "\n" + linux_result["timer_text"]
    ).splitlines()[:30]
    for line in head:
        print(line)
