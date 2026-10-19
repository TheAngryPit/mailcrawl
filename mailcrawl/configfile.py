# SPDX-License-Identifier: MIT
"""Read and write the local config file."""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
import unicodedata
import tomllib
from pathlib import Path

from mailcrawl.clock import require_as_of
from mailcrawl.inference import BoundaryError, enabled_points
from mailcrawl.paths import UsageError, canonical_alias
from mailcrawl.textnorm import parse_address


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


def takeout_label_map(path: Path, alias: str) -> dict[str, str]:
    document = load_config(path)
    block = document.get("takeout")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise UsageError("takeout label_map needs a table")
    found: dict[str, str] = {}
    _merge_label_map(found, block.get("label_map"))
    account = block.get(canonical_alias(alias))
    if isinstance(account, dict):
        _merge_label_map(found, account.get("label_map"))
    return found


def _merge_label_map(found: dict[str, str], value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise UsageError("takeout label_map needs a table")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise UsageError("takeout label_map needs a table")
        found[key] = item


def taxonomy_path(path: Path) -> Path | None:
    document = load_config(path)
    value = document.get("taxonomy")
    if not isinstance(value, str) or not value:
        return None
    return Path(value)


DEFAULT_BATCH_SIZE = 1
DEFAULT_MAX_TOKENS = 2048


DEVICES = ("auto", "mps", "cuda", "cpu")


def inference_config(path: Path) -> dict[str, bool]:
    raw = _inference_table(path)
    embed_limits(path)
    inference_device(path)
    flags: dict[str, bool] = {}
    for key, value in raw.items():
        if key == "embedder":
            _embedder_value(value)
            continue
        if key in ("batch_size", "max_tokens", "llm", "device"):
            continue
        if not isinstance(key, str) or not isinstance(value, bool):
            raise UsageError("inference flags are booleans")
        flags[key] = value
    try:
        enabled_points(flags)
    except BoundaryError as exc:
        raise UsageError(str(exc)) from exc
    return flags


def embed_limits(path: Path) -> tuple[int, int]:
    raw = _inference_table(path)
    return (
        _optional_count(raw, "batch_size", DEFAULT_BATCH_SIZE),
        _optional_count(raw, "max_tokens", DEFAULT_MAX_TOKENS),
    )


def configured_as_of(path: Path) -> int | None:
    value = load_config(path).get("as_of")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageError("as_of needs a time")
    return require_as_of(value)


def time_policy(path: Path) -> TimePolicy:
    from mailcrawl.timefacts import OLD_DAYS, STALE_DAYS, TimePolicy

    document = load_config(path)
    block = document.get("time")
    if block is None:
        return TimePolicy()
    if not isinstance(block, dict):
        raise UsageError("time config is not a table")
    return TimePolicy(
        stale_days=_optional_days(block, "stale_days", STALE_DAYS),
        old_days=_optional_days(block, "old_days", OLD_DAYS),
    )


def allow_override(path: Path) -> bool:
    value = _review_block(path).get("allow_override", False)
    if not isinstance(value, bool):
        raise UsageError("allow_override is a boolean")
    return value


def llm_review(path: Path) -> bool:
    value = _review_block(path).get("llm_review", False)
    if not isinstance(value, bool):
        raise UsageError("llm_review is a boolean")
    return value


def _review_block(path: Path) -> dict[str, object]:
    document = load_config(path)
    block = document.get("review")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise UsageError("review config is not a table")
    return block


def local_llm(path: Path) -> tuple[str, str, bool] | None:
    raw = _inference_table(path)
    block = raw.get("llm")
    if block is None or block is False:
        return None
    if not isinstance(block, dict):
        raise UsageError("local llm config is not a table")
    base_url = block.get("base_url")
    model = block.get("model")
    if not isinstance(base_url, str) or not base_url.strip():
        raise UsageError("local llm needs a base_url")
    if not isinstance(model, str) or not model.strip():
        raise UsageError("local llm needs a model")
    allow_remote = block.get("allow_remote", False)
    if not isinstance(allow_remote, bool):
        raise UsageError("allow_remote is a boolean")
    return base_url.strip(), model.strip(), allow_remote


def inference_device(path: Path) -> str:
    raw = _inference_table(path)
    if "device" not in raw:
        return "auto"
    return _device_value(raw["device"])


def owner_addresses(path: Path, alias: str) -> frozenset[str]:
    document = load_config(path)
    accounts = document.get("accounts")
    if not isinstance(accounts, dict):
        return frozenset()
    row = accounts.get(canonical_alias(alias))
    if not isinstance(row, dict):
        return frozenset()
    value = row.get("owner_addresses")
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise UsageError("owner_addresses needs a list")
    found: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise UsageError("owner address is not an email")
        _name, address, _domain = parse_address(item)
        if address is None:
            raise UsageError("owner address is not an email")
        found.add(_fold_address(address))
    return frozenset(found)


def rules_path(path: Path) -> Path | None:
    document = load_config(path)
    if "rules" not in document:
        return None
    block = document.get("rules")
    if isinstance(block, str):
        return _locate_rules(path, _rules_value(block))
    if not isinstance(block, dict):
        raise UsageError("rules config is not a table")
    value = block.get("path")
    if value is None:
        return None
    return _locate_rules(path, _rules_value(value))


def _locate_rules(config: Path, raw: str) -> Path:
    expanded = Path(raw).expanduser()
    if expanded.is_absolute():
        return expanded
    return config.parent / expanded


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
    return _config_string(value, "embedder")


def _rules_value(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UsageError("rules is not a path")
    if '"' in value or "\n" in value:
        raise UsageError("rules contains a character the config file cannot store")
    return value.strip()


def _device_value(value: object) -> str:
    if not isinstance(value, str) or value not in DEVICES:
        raise UsageError("device is auto, mps, cuda, or cpu")
    return value


def _config_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UsageError(f"{name} is not a name")
    if '"' in value or "\n" in value:
        raise UsageError(f"{name} contains a character the config file cannot store")
    return value.strip()


def _optional_days(raw: dict[str, object], key: str, default: int) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise UsageError(f"{key} needs a positive count")
    return value


def _optional_count(raw: dict[str, object], key: str, default: int) -> int:
    if key not in raw:
        return default
    return _required_count(raw[key], key)


def _required_count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise UsageError(f"{name} needs a positive count")
    return value


def write_account(path: Path, alias: str, email: str) -> None:
    canonical = canonical_alias(alias)
    if '"' in email or "\n" in email:
        raise UsageError("email contains a character the config file cannot store")
    document = load_config(path)
    accounts = document.get("accounts")
    if accounts is None:
        accounts = {}
    if not isinstance(accounts, dict):
        raise UsageError("accounts config is not a table")
    current = accounts.get(canonical)
    if current is not None and not isinstance(current, dict):
        raise UsageError("account config is not a table")
    row = dict(current) if isinstance(current, dict) else {}
    row["email"] = email.strip()
    accounts[canonical] = row
    document["accounts"] = accounts
    legacy_rules = document.get("rules")
    if isinstance(legacy_rules, str) and legacy_rules.strip():
        document["rules"] = {"path": _rules_value(legacy_rules)}
    content = "\n".join(_toml_table(document)) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _toml_value(value)


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{_toml_key(key)} = {_toml_value(item)}" for key, item in value.items()
        ) + " }"
    raise UsageError("config contains an unsupported TOML value")


def _toml_table(document: dict[str, object], prefix: tuple[str, ...] = ()) -> list[str]:
    lines = ["[" + ".".join(_toml_key(key) for key in prefix) + "]"] if prefix else []
    for key, value in document.items():
        if not isinstance(value, dict):
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in document.items():
        if isinstance(value, dict):
            lines.extend(["", *_toml_table(value, (*prefix, key))])
    return lines


def _fold_address(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


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
