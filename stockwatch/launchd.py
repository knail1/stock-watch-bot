"""Install/uninstall the StockWatch LaunchAgent (spec section 9).

Renders `deploy/com.local.stockwatch.plist.template` into
`~/Library/LaunchAgents/com.local.stockwatch.plist` and loads it with
`launchctl bootstrap gui/<uid>`. KeepAlive + ThrottleInterval 10 means launchd
restarts a killed service well within 30 seconds.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from . import paths

LABEL = "com.local.stockwatch"
TEMPLATE_NAME = f"{LABEL}.plist.template"
DEFAULT_PATH_ENV = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class LaunchdError(RuntimeError):
    pass


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=30)


def template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "deploy" / TEMPLATE_NAME


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def domain() -> str:
    return f"gui/{os.getuid()}"


def service_target() -> str:
    return f"{domain()}/{LABEL}"


def default_path_env(python: str) -> str:
    """PATH for the agent: the venv's bin first (so `stockwatch` helpers resolve), then system dirs."""
    parts = [str(Path(python).parent), *DEFAULT_PATH_ENV.split(":")]
    return ":".join(dict.fromkeys(parts))


def render_plist(
    python: str | None = None,
    working_dir: Path | None = None,
    log_dir: Path | None = None,
    path_env: str | None = None,
    stockwatch_home: Path | None = None,
    template: str | None = None,
) -> str:
    """Fill the template's {{PLACEHOLDERS}} with XML-escaped values."""
    python = python or sys.executable
    home = stockwatch_home or paths.home_dir()
    values = {
        "LABEL": LABEL,
        "PYTHON": python,
        "WORKING_DIR": str(working_dir or home),
        "LOG_DIR": str(log_dir or paths.logs_dir()),
        "PATH": path_env or default_path_env(python),
        "STOCKWATCH_HOME": str(home),
    }
    if template is None:
        tp = template_path()
        if not tp.exists():
            raise LaunchdError(f"plist template missing at {tp} (install the package in editable mode)")
        template = tp.read_text()
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", escape(value))
    if "{{" in template:
        raise LaunchdError("unfilled placeholder in plist template")
    return template


@dataclass
class InstallResult:
    plist: Path
    loaded: bool
    detail: str = ""


def is_loaded(run: Runner = _run) -> bool:
    return run(["launchctl", "print", service_target()]).returncode == 0


def install(
    python: str | None = None,
    run: Runner = _run,
    dest: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> InstallResult:
    """Write the plist and (re)load it. Safe to run repeatedly."""
    dest = dest or plist_path()
    paths.ensure_dirs()
    content = render_plist(python=python)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    os.chmod(dest, 0o644)
    # Unload any previous copy first; bootstrap fails if the label is already loaded.
    was_loaded = is_loaded(run)
    if was_loaded:
        run(["launchctl", "bootout", service_target()])
    # bootout completes asynchronously; bootstrap may briefly fail with EIO right after it.
    for attempt in range(3 if was_loaded else 1):
        if attempt:
            sleep(1.0)
        res = run(["launchctl", "bootstrap", domain(), str(dest)])
        if res.returncode == 0:
            break
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        return InstallResult(dest, False, f"launchctl bootstrap failed ({res.returncode}): {detail}")
    run(["launchctl", "enable", service_target()])
    return InstallResult(dest, True, f"loaded {LABEL}; logs in {paths.logs_dir()}")


def uninstall(run: Runner = _run, dest: Path | None = None) -> InstallResult:
    """Unload the agent (if loaded) and remove the plist."""
    dest = dest or plist_path()
    detail = "not loaded"
    if is_loaded(run):
        res = run(["launchctl", "bootout", service_target()])
        detail = "unloaded" if res.returncode == 0 else f"bootout failed: {(res.stderr or '').strip()}"
    if dest.exists():
        dest.unlink()
        detail += f"; removed {dest}"
    return InstallResult(dest, False, detail)


def restart(run: Runner = _run) -> bool:
    """Kill and restart the running agent (`launchctl kickstart -k`)."""
    return run(["launchctl", "kickstart", "-k", service_target()]).returncode == 0
