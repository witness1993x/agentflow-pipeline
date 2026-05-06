"""Tests for ``agentflow_pipeline.schedule_installer``.

All filesystem and ``subprocess`` interactions are monkeypatched so the
real ``~/Library/LaunchAgents`` and ``~/.config/systemd/user`` are never
touched.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from agentflow_pipeline import schedule_installer as si


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_spec(tmp_path: Path) -> si.ScheduleSpec:
    """Return a :class:`ScheduleSpec` rooted at a tmp host directory."""
    root = tmp_path / "host"
    root.mkdir()
    return si.ScheduleSpec(
        label="com.agentflow.scan.test",
        times=["09:00", "21:00"],
        command=["agentflow-scan", "--root", str(root)],
        working_dir=root,
        log_dir=root / "trends" / "_logs",
        description="test schedule",
    )


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect ``Path.home()`` and ``$HOME`` / ``$XDG_CONFIG_HOME`` to tmp.

    Guarantees install/uninstall tests cannot stomp on the real user's
    LaunchAgents or systemd unit dirs.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


class _FakeCP:
    """Tiny stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# detect_platform
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fake,expected",
    [
        ("darwin", "macos"),
        ("linux", "linux"),
        ("linux2", "linux"),
        ("win32", "unknown"),
        ("freebsd", "unknown"),
    ],
)
def test_detect_platform_returns_expected(
    monkeypatch: pytest.MonkeyPatch, fake: str, expected: str
) -> None:
    monkeypatch.setattr(si.sys, "platform", fake)
    assert si.detect_platform() == expected


# ---------------------------------------------------------------------------
# build_macos_plist
# ---------------------------------------------------------------------------


def test_build_macos_plist_contains_calendar_intervals(sample_spec: si.ScheduleSpec) -> None:
    text = si.build_macos_plist(sample_spec)
    # XML preamble
    assert text.startswith("<?xml")
    # Two times -> two <dict> entries inside the StartCalendarInterval array
    assert text.count("<key>Hour</key>") == 2
    assert text.count("<key>Minute</key>") == 2
    # Specific values
    assert "<integer>9</integer>" in text
    assert "<integer>21</integer>" in text
    assert "<integer>0</integer>" in text


def test_build_macos_plist_contains_program_arguments_and_paths(
    sample_spec: si.ScheduleSpec,
) -> None:
    text = si.build_macos_plist(sample_spec)
    assert "<key>ProgramArguments</key>" in text
    # Each argv entry is rendered as <string>
    for arg in sample_spec.command:
        assert f"<string>{arg}</string>" in text
    assert "<key>StandardOutPath</key>" in text
    assert "<key>StandardErrorPath</key>" in text
    assert str(sample_spec.log_dir / f"{sample_spec.label}.out.log") in text
    assert str(sample_spec.log_dir / f"{sample_spec.label}.err.log") in text
    assert "<key>Label</key>" in text
    assert f"<string>{sample_spec.label}</string>" in text
    assert "<key>RunAtLoad</key>" in text
    assert "<key>KeepAlive</key>" in text


# ---------------------------------------------------------------------------
# build_systemd_units
# ---------------------------------------------------------------------------


def test_build_systemd_units_service_and_timer(sample_spec: si.ScheduleSpec) -> None:
    service, timer = si.build_systemd_units(sample_spec)

    assert "[Service]" in service
    assert "Type=oneshot" in service
    assert "ExecStart=" in service
    # Command is echoed
    assert "agentflow-scan" in service
    assert str(sample_spec.working_dir) in service

    # Timer should have one OnCalendar per HH:MM
    assert "[Timer]" in timer
    assert "OnCalendar=*-*-* 09:00:00" in timer
    assert "OnCalendar=*-*-* 21:00:00" in timer
    assert f"Unit={sample_spec.label}.service" in timer
    assert "Persistent=true" in timer


# ---------------------------------------------------------------------------
# build_default_scan_spec
# ---------------------------------------------------------------------------


def test_build_default_scan_spec_defaults(tmp_path: Path) -> None:
    spec = si.build_default_scan_spec(root=tmp_path)
    assert spec.label == "com.agentflow.scan.daily"
    assert spec.times == ["09:00", "21:00"]
    # log_dir convention
    assert spec.log_dir == tmp_path.resolve() / "trends" / "_logs"
    # command always contains --root <root>
    assert "--root" in spec.command
    assert str(tmp_path.resolve()) in spec.command


def test_build_default_scan_spec_custom_args(tmp_path: Path) -> None:
    spec = si.build_default_scan_spec(
        root=tmp_path,
        label="custom.label",
        times=["12:00"],
        scan_extra_args=["--sources", "github,hackernews"],
    )
    assert spec.label == "custom.label"
    assert spec.times == ["12:00"]
    assert spec.command[-2:] == ["--sources", "github,hackernews"]


# ---------------------------------------------------------------------------
# install_macos_launchd: dry-run
# ---------------------------------------------------------------------------


def test_install_macos_launchd_dry_run_does_not_touch_filesystem(
    sample_spec: si.ScheduleSpec, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[Any] = []

    def boom_run(*args: Any, **kwargs: Any) -> Any:
        called.append(args)
        raise AssertionError("subprocess.run must not be called in dry-run mode")

    monkeypatch.setattr(si.subprocess, "run", boom_run)

    plist_path = si._macos_plist_path(sample_spec.label)
    assert not plist_path.exists()

    result = si.install_macos_launchd(sample_spec, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["plist_path"] == str(plist_path)
    assert not plist_path.exists(), "dry-run must not write the plist"
    assert not sample_spec.log_dir.exists(), "dry-run must not mkdir the log dir"
    assert any("would write plist" in a for a in result["actions"])
    assert any("launchctl bootstrap" in a for a in result["actions"])
    assert called == []


# ---------------------------------------------------------------------------
# install_macos_launchd: real write path (with monkeypatched subprocess)
# ---------------------------------------------------------------------------


def test_install_macos_launchd_apply_writes_plist_and_calls_bootstrap(
    sample_spec: si.ScheduleSpec, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCP:
        runs.append(list(cmd))
        return _FakeCP(returncode=0)

    monkeypatch.setattr(si.subprocess, "run", fake_run)

    result = si.install_macos_launchd(sample_spec, dry_run=False)

    plist_path = Path(result["plist_path"])
    assert plist_path.parent == isolated_home / "Library" / "LaunchAgents"
    assert plist_path.exists(), "real install must write the plist"
    assert plist_path.read_text(encoding="utf-8").startswith("<?xml")
    assert sample_spec.log_dir.exists(), "real install must create the log dir"
    assert result["status"] == "installed"
    # bootstrap was invoked
    assert any(cmd[:2] == ["launchctl", "bootstrap"] for cmd in runs)


def test_install_macos_launchd_existing_plist_without_force_returns_exists(
    sample_spec: si.ScheduleSpec, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plist_path = si._macos_plist_path(sample_spec.label)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text("<old/>", encoding="utf-8")

    runs: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCP:
        runs.append(list(cmd))
        return _FakeCP(returncode=0)

    monkeypatch.setattr(si.subprocess, "run", fake_run)

    result = si.install_macos_launchd(sample_spec, dry_run=False, force=False)

    assert result["status"] == "exists"
    assert plist_path.read_text(encoding="utf-8") == "<old/>"
    assert runs == [], "existing plist without --force must not call launchctl"


# ---------------------------------------------------------------------------
# install_systemd_timer
# ---------------------------------------------------------------------------


def test_install_systemd_timer_apply_writes_units_and_invokes_systemctl(
    sample_spec: si.ScheduleSpec, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCP:
        runs.append(list(cmd))
        return _FakeCP(returncode=0)

    monkeypatch.setattr(si.subprocess, "run", fake_run)

    result = si.install_systemd_timer(sample_spec, dry_run=False, user_scope=True)

    service_path = Path(result["service_path"])
    timer_path = Path(result["timer_path"])
    assert service_path.exists()
    assert timer_path.exists()
    assert "ExecStart=" in service_path.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 09:00:00" in timer_path.read_text(encoding="utf-8")
    assert result["status"] == "installed"

    flat = [" ".join(cmd) for cmd in runs]
    assert any("systemctl --user daemon-reload" in line for line in flat)
    assert any(
        "systemctl --user enable --now com.agentflow.scan.test.timer" in line
        for line in flat
    )


def test_install_systemd_timer_dry_run_does_not_touch_filesystem(
    sample_spec: si.ScheduleSpec, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be called in dry-run mode")

    monkeypatch.setattr(si.subprocess, "run", boom_run)

    result = si.install_systemd_timer(sample_spec, dry_run=True, user_scope=True)

    assert result["status"] == "dry_run"
    assert not Path(result["service_path"]).exists()
    assert not Path(result["timer_path"]).exists()


# ---------------------------------------------------------------------------
# uninstall_*
# ---------------------------------------------------------------------------


def test_uninstall_macos_launchd_dry_run_no_subprocess_no_unlink(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    label = "com.agentflow.scan.test"
    plist_path = si._macos_plist_path(label)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text("<placeholder/>", encoding="utf-8")

    def boom_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run must not call subprocess.run")

    monkeypatch.setattr(si.subprocess, "run", boom_run)

    result = si.uninstall_macos_launchd(label, dry_run=True)

    assert result["status"] == "dry_run"
    assert plist_path.exists(), "dry-run must not unlink the plist"


def test_uninstall_systemd_timer_dry_run_no_subprocess_no_unlink(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    label = "com.agentflow.scan.test"
    unit_dir = si._systemd_unit_dir(user_scope=True)
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = unit_dir / f"{label}.service"
    timer = unit_dir / f"{label}.timer"
    service.write_text("placeholder", encoding="utf-8")
    timer.write_text("placeholder", encoding="utf-8")

    def boom_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run must not call subprocess.run")

    monkeypatch.setattr(si.subprocess, "run", boom_run)

    result = si.uninstall_systemd_timer(label, dry_run=True, user_scope=True)

    assert result["status"] == "dry_run"
    assert service.exists() and timer.exists()


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------


def test_main_install_dry_run_default_prints_plist_and_does_not_touch_fs(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(si.sys, "platform", "darwin")

    def boom_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("default --dry-run path must not call subprocess.run")

    monkeypatch.setattr(si.subprocess, "run", boom_run)

    root = tmp_path / "myhost"
    root.mkdir()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = si.main(["install", "--root", str(root)])

    out = buf.getvalue()
    assert rc == 0
    assert "status   = dry_run" in out
    # Plist content is dumped for the user to review
    assert "<key>ProgramArguments</key>" in out
    assert "<key>StartCalendarInterval</key>" in out
    # No actual plist file written
    assert not si._macos_plist_path("com.agentflow.scan.daily").exists()


def test_main_install_apply_actually_writes_plist(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(si.sys, "platform", "darwin")

    runs: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCP:
        runs.append(list(cmd))
        return _FakeCP(returncode=0)

    monkeypatch.setattr(si.subprocess, "run", fake_run)

    root = tmp_path / "applyhost"
    root.mkdir()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = si.main(["install", "--root", str(root), "--apply"])

    out = buf.getvalue()
    assert rc == 0
    assert "status   = installed" in out
    plist_path = si._macos_plist_path("com.agentflow.scan.daily")
    assert plist_path.exists()
    # bootstrap actually invoked
    assert any(cmd[:2] == ["launchctl", "bootstrap"] for cmd in runs)


def test_main_install_systemd_dry_run_prints_units(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(si.sys, "platform", "linux")

    def boom_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("default --dry-run path must not call subprocess.run")

    monkeypatch.setattr(si.subprocess, "run", boom_run)

    root = tmp_path / "linhost"
    root.mkdir()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = si.main(["install", "--root", str(root)])

    out = buf.getvalue()
    assert rc == 0
    assert "status   = dry_run" in out
    assert "OnCalendar=*-*-* 09:00:00" in out
    assert "OnCalendar=*-*-* 21:00:00" in out
    # Custom times CLI override would be: --times "12:00"
    # we leave that to a separate test below.


# ---------------------------------------------------------------------------
# daemon mode (TG callback listener)
# ---------------------------------------------------------------------------


def test_build_default_listener_spec_returns_daemon_mode(tmp_path: Path) -> None:
    spec = si.build_default_listener_spec(
        root=tmp_path,
        listener_extra_args=["--bot-token-env", "TELEGRAM_BOT_TOKEN"],
    )
    assert spec.mode == "daemon"
    assert spec.label == "com.agentflow.tg-listener"
    # log_dir convention matches scan jobs
    assert spec.log_dir == tmp_path.resolve() / "trends" / "_logs"
    # working_dir is the resolved root
    assert spec.working_dir == tmp_path.resolve()
    # command targets the listener binary with --root and the extra args
    assert "agentflow-tg-listen" in spec.command[0]
    assert "--root" in spec.command
    assert str(tmp_path.resolve()) in spec.command
    assert "--bot-token-env" in spec.command
    assert "TELEGRAM_BOT_TOKEN" in spec.command
    # daemon mode does not use the times list
    assert spec.times == []


def test_build_macos_plist_daemon_mode_has_keepalive_dict_no_calendar(
    tmp_path: Path,
) -> None:
    spec = si.build_default_listener_spec(root=tmp_path)
    text = si.build_macos_plist(spec)

    # daemon plists run on load + keep alive + throttle restart
    assert "<key>RunAtLoad</key>" in text
    assert "<key>KeepAlive</key>" in text
    assert "<key>ThrottleInterval</key>" in text
    assert "<integer>10</integer>" in text
    # KeepAlive must be a dict so launchd respects clean exits
    assert "<key>SuccessfulExit</key>" in text
    assert "<key>Crashed</key>" in text
    assert "<key>NetworkState</key>" in text
    # Definitely no cron-style schedule
    assert "<key>StartCalendarInterval</key>" not in text
    # RunAtLoad must be true (cron mode would be false)
    run_at_load_idx = text.index("<key>RunAtLoad</key>")
    after = text[run_at_load_idx : run_at_load_idx + 80]
    assert "<true/>" in after


def test_build_systemd_units_daemon_mode_simple_service_no_timer(
    tmp_path: Path,
) -> None:
    spec = si.build_default_listener_spec(root=tmp_path)
    service, timer = si.build_systemd_units(spec)

    assert "[Service]" in service
    assert "Type=simple" in service
    assert "Restart=on-failure" in service
    assert "RestartSec=10" in service
    # cron-only directives must not leak into daemon service
    assert "Type=oneshot" not in service
    # No timer companion in daemon mode
    assert timer == ""


def test_main_install_custom_times_and_scan_args(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(si.sys, "platform", "darwin")
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: _FakeCP())

    root = tmp_path / "customhost"
    root.mkdir()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = si.main(
            [
                "install",
                "--root",
                str(root),
                "--times",
                "07:30,18:45",
                "--scan-args",
                "--sources github,hackernews",
            ]
        )
    out = buf.getvalue()
    assert rc == 0
    # Times reflected in the dumped plist
    assert "<integer>7</integer>" in out
    assert "<integer>30</integer>" in out
    assert "<integer>18</integer>" in out
    assert "<integer>45</integer>" in out
    # Extra scan args present
    assert "<string>--sources</string>" in out
    assert "<string>github,hackernews</string>" in out
