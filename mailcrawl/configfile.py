"""Read and write the local config file."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from mailcrawl.inference import BoundaryError, enabled_points
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


def account_identity(
    path: Path, alias: str
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    document = load_config(path)
    accounts = document.get("accounts")
    empty = frozenset(), frozenset(), frozenset(), frozenset()
    if not isinstance(accounts, dict):
        return empty
    row = accounts.get(canonical_alias(alias))
    if not isinstance(row, dict):
        return empty
    return (
        _address_set(row.get("owners")),
        _address_set(row.get("domains")),
        _address_set(row.get("security_senders")),
        _address_set(row.get("test_recipients")),
    )


def taxonomy_path(path: Path) -> Path | None:
    document = load_config(path)
    value = document.get("taxonomy")
    if not isinstance(value, str) or not value:
        return None
    return Path(value)


def inference_config(path: Path) -> dict[str, bool]:
    raw = _inference_table(path)
    flags: dict[str, bool] = {}
    for key, value in raw.items():
        if key == "embedder":
            _embedder_value(value)
            continue
        if not isinstance(key, str) or not isinstance(value, bool):
            raise UsageError("inference flags are booleans")
        flags[key] = value
    try:
        enabled_points(flags)
    except BoundaryError as exc:
        raise UsageError(str(exc)) from exc
    return flags


def embedder_name(path: Path) -> str:
    raw = _inference_table(path)
    if "embedder" not in raw:
        return "gemma"
    return _embedder_value(raw["embedder"])


def _inference_table(path: Path) -> dict[str, object]:
    document = load_config(path)
    raw = document.get("inference")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise UsageError("inference config is not a table")
    return raw


def _embedder_value(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UsageError("embedder is not a name")
    return value.strip()


def write_account(path: Path, alias: str, email: str) -> None:
    canonical = canonical_alias(alias)
    if '"' in email or "\n" in email:
        raise UsageError("email contains a character the config file cannot store")
    document = load_config(path)
    accounts = document.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    current = accounts.get(canonical)
    row = dict(current) if isinstance(current, dict) else {}
    row["email"] = email.strip()
    accounts[canonical] = row
    taxonomy = document.get("taxonomy")
    lines: list[str] = []
    if isinstance(taxonomy, str) and taxonomy and '"' not in taxonomy:
        lines.append(f'taxonomy = "{taxonomy}"')
        lines.append("")
    inference = document.get("inference")
    if isinstance(inference, dict):
        saved = [
            (key, value)
            for key, value in inference.items()
            if isinstance(key, str) and isinstance(value, bool)
        ]
        named = _embedder_value(inference["embedder"]) if "embedder" in inference else None
        if saved or named:
            lines.append("[inference]")
            if named:
                if '"' in named or "\n" in named:
                    raise UsageError("embedder contains a character the config file cannot store")
                lines.append(f'embedder = "{named}"')
            for key, value in sorted(saved):
                lines.append(f"{key} = {'true' if value else 'false'}")
            lines.append("")
    for name in sorted(accounts):
        account = accounts[name]
        if not isinstance(account, dict):
            continue
        addr = account.get("email")
        if not isinstance(addr, str):
            continue
        lines.append(f"[accounts.{name}]")
        lines.append(f'email = "{addr}"')
        owners = _string_list(account.get("owners"))
        domains = _string_list(account.get("domains"))
        security_senders = _string_list(account.get("security_senders"))
        test_recipients = _string_list(account.get("test_recipients"))
        if owners:
            lines.append(f"owners = {_toml_strings(owners)}")
        if domains:
            lines.append(f"domains = {_toml_strings(domains)}")
        if security_senders:
            lines.append(f"security_senders = {_toml_strings(security_senders)}")
        if test_recipients:
            lines.append(f"test_recipients = {_toml_strings(test_recipients)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(path, 0o600)


def _address_set(value: object) -> frozenset[str]:
    return frozenset(item.lower() for item in _string_list(value))


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    found: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        if '"' in item or "\n" in item:
            raise UsageError("owner address contains a character the config file cannot store")
        found.append(item.strip())
    return tuple(found)


def _toml_strings(values: tuple[str, ...]) -> str:
    inner = ", ".join(f'"{item}"' for item in values)
    return f"[{inner}]"
