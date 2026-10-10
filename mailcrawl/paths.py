"""Account aliases and on-disk locations."""

from __future__ import annotations

import os
from pathlib import Path

ALIASES = frozenset({"thehive", "vcl", "personal"})


class UsageError(Exception):
    """The command line or config is not usable."""


def canonical_alias(alias: str) -> str:
    key = alias.strip().lower()
    if key == "pessoal":
        key = "personal"
    if key not in ALIASES:
        raise UsageError(f"unknown account alias {alias}")
    return key


def data_dir(home_flag: str | None) -> Path:
    if home_flag:
        return Path(home_flag)
    env = os.environ.get("MAILCRAWL_HOME")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "mailcrawl"


def config_path(config_flag: str | None) -> Path:
    if config_flag:
        return Path(config_flag)
    return Path.home() / ".config" / "mailcrawl" / "config.toml"
