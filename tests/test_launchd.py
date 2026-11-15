"""Tests for plist rendering and launchctl install/uninstall (launchctl is faked)."""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from stockwatch import launchd


class FakeLaunchctl:
    def __init__(self, loaded: bool = False, bootstrap_rc: Sequence[int] = (0,)) -> None:
        self.loaded = loaded
        self.bootstrap_rc = list(bootstrap_rc)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        cmd = list(cmd)
        self.calls.append(cmd)
        rc = 0
        if cmd[1] == "print":
            rc = 0 if self.loaded else 113
        elif cmd[1] == "bootout":
            self.loaded = False
        elif cmd[1] == "bootstrap":
            rc = self.bootstrap_rc.pop(0) if self.bootstrap_rc else 0
            self.loaded = rc == 0
        return subprocess.CompletedProcess(cmd, rc, "", "Bootstrap failed: 5: Input/output error" if rc else "")

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


@pytest.fixture(autouse=True)
def sw_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "StockWatch"
    monkeypatch.setenv("STOCKWATCH_HOME", str(home))
    return home


def test_render_plist_is_valid_and_meets_restart_requirements(sw_home: Path) -> None:
    xml = launchd.render_plist(python="/opt/sw/.venv/bin/python")
    pl = plistlib.loads(xml.encode())
    assert pl["Label"] == "com.local.stockwatch"
    assert pl["ProgramArguments"] == ["/opt/sw/.venv/bin/python", "-m", "stockwatch", "run"]
    assert pl["RunAtLoad"] is True and pl["KeepAlive"] is True
    assert pl["ThrottleInterval"] < 30
    assert pl["WorkingDirectory"] == str(sw_home)
    assert pl["StandardOutPath"].startswith(str(sw_home / "logs"))
    assert pl["StandardErrorPath"].startswith(str(sw_home / "logs"))
    env = pl["EnvironmentVariables"]
    assert env["PATH"].split(":")[0] == "/opt/sw/.venv/bin" and "/usr/bin" in env["PATH"]
    assert env["STOCKWATCH_HOME"] == str(sw_home)


def test_render_escapes_xml(tmp_path: Path) -> None:
    xml = launchd.render_plist(python="/tmp/a&b <x>/python", working_dir=tmp_path, log_dir=tmp_path)
    assert plistlib.loads(xml.encode())["ProgramArguments"][0] == "/tmp/a&b <x>/python"


def test_render_rejects_unknown_placeholder() -> None:
    with pytest.raises(launchd.LaunchdError):
        launchd.render_plist(python="/p", template="<x>{{NOPE}}</x>")


def test_install_fresh(tmp_path: Path) -> None:
    fake = FakeLaunchctl()
    dest = tmp_path / "LaunchAgents" / "com.local.stockwatch.plist"
    res = launchd.install(python="/p/python", run=fake, dest=dest)
    assert res.loaded and dest.exists()
    assert oct(dest.stat().st_mode & 0o777) == "0o644"
    assert fake.verbs() == ["print", "bootstrap", "enable"]
    assert fake.calls[1] == ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(dest)]


def test_reinstall_boots_out_and_retries(tmp_path: Path) -> None:
    fake = FakeLaunchctl(loaded=True, bootstrap_rc=(5, 0))
    sleeps: list[float] = []
    dest = tmp_path / "agent.plist"
    res = launchd.install(python="/p/python", run=fake, dest=dest, sleep=sleeps.append)
    assert res.loaded
    assert fake.verbs() == ["print", "bootout", "bootstrap", "bootstrap", "enable"]
    assert fake.calls[1] == ["launchctl", "bootout", f"gui/{os.getuid()}/com.local.stockwatch"]
    assert sleeps == [1.0]


def test_install_reports_bootstrap_failure(tmp_path: Path) -> None:
    fake = FakeLaunchctl(bootstrap_rc=(5,))
    res = launchd.install(python="/p/python", run=fake, dest=tmp_path / "a.plist")
    assert not res.loaded and "Input/output error" in res.detail


def test_uninstall(tmp_path: Path) -> None:
    dest = tmp_path / "a.plist"
    dest.write_text("x")
    fake = FakeLaunchctl(loaded=True)
    res = launchd.uninstall(run=fake, dest=dest)
    assert not dest.exists() and "unloaded" in res.detail
    assert fake.verbs() == ["print", "bootout"]
    # second uninstall is a harmless no-op
    res = launchd.uninstall(run=fake, dest=dest)
    assert "not loaded" in res.detail
