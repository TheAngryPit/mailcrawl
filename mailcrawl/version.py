# SPDX-License-Identifier: MIT
"""Package version and provenance hash."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

VERSION = "0.1.0"
_SHA = re.compile(r"[0-9a-f]{7,40}")


def mailcrawl_version() -> str:
    if not (Path(__file__).resolve().parents[1] / ".git").exists():
        return VERSION
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return VERSION
    if _SHA.fullmatch(out):
        return f"{VERSION}+{out}"
    return VERSION


def provenance_hash(
    alias: str,
    gmail_message_id: str,
    raw_sha256: str,
    version: str,
) -> str:
    payload = "\n".join(("gog", alias, gmail_message_id, raw_sha256, version))
    return hashlib.sha256(payload.encode()).hexdigest()
