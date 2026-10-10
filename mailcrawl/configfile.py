"""Read and write the local config file."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from mailcrawl.paths import UsageError, canonical_alias


def load_config(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise UsageError("config file is not a table")
    return document


def account_email(path: Path, alias: str) -> str:
    document = load_config(path)
    accounts = document.get("accounts")
    if not isinstance(accounts, dict):
        raise UsageError("account is not initialized")
    row = accounts.get(alias)
    if not isinstance(row, dict):
        raise UsageError("account is not initialized")
    email = row.get("email")
    if not isinstance(email, str) or not email.strip():
        raise UsageError("account is not initialized")
    return email.strip()


def taxonomy_path(path: Path) -> Path | None:
    document = load_config(path)
    value = document.get("taxonomy")
    if not isinstance(value, str) or not value:
        return None
    return Path(value)


def write_account(path: Path, alias: str, email: str) -> None:
    canonical = canonical_alias(alias)
    if '"' in email or "\n" in email:
        raise UsageError("email contains a character the config file cannot store")
    document = load_config(path)
    accounts = document.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    accounts[canonical] = {"email": email.strip()}
    taxonomy = document.get("taxonomy")
    lines: list[str] = []
    if isinstance(taxonomy, str) and taxonomy and '"' not in taxonomy:
        lines.append(f'taxonomy = "{taxonomy}"')
        lines.append("")
    for name in sorted(accounts):
        row = accounts[name]
        if not isinstance(row, dict):
            continue
        addr = row.get("email")
        if not isinstance(addr, str):
            continue
        lines.append(f"[accounts.{name}]")
        lines.append(f'email = "{addr}"')
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(path, 0o600)
