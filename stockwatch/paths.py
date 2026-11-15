"""Filesystem locations. Everything lives under ~/StockWatch unless STOCKWATCH_HOME is set."""
from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    return Path(os.environ.get("STOCKWATCH_HOME", Path.home() / "StockWatch")).expanduser()


def config_path() -> Path:
    return Path(os.environ.get("STOCKWATCH_CONFIG", home_dir() / "config.yaml")).expanduser()


def env_path() -> Path:
    return home_dir() / ".env"


def db_path() -> Path:
    return home_dir() / "stockwatch.db"


def logs_dir() -> Path:
    return home_dir() / "logs"


def chrome_profile_dir() -> Path:
    return home_dir() / "chrome-profile"


def assets_dir() -> Path:
    """Bundled assets (alarm sound) shipped with the repo."""
    return Path(__file__).resolve().parent.parent / "assets"


def ensure_dirs() -> None:
    """Create runtime dirs with safe permissions (spec section 12)."""
    home_dir().mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    prof = chrome_profile_dir()
    prof.mkdir(parents=True, exist_ok=True)
    os.chmod(prof, 0o700)
    if env_path().exists():
        os.chmod(env_path(), 0o600)
