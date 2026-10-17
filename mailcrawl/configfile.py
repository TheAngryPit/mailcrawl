"""Read and write the local config file."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from mailcrawl.clock import require_as_of
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
    document = load_config(path)
    block = document.get("review")
    if block is None:
        return False
    if not isinstance(block, dict):
        raise UsageError("review config is not a table")
    value = block.get("allow_override", False)
    if not isinstance(value, bool):
        raise UsageError("allow_override is a boolean")
    return value


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


def rules_path(path: Path) -> Path | None:
    document = load_config(path)
    if "rules" not in document:
        return None
    return Path(_rules_value(document.get("rules"))).expanduser()


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
    if not isinstance(accounts, dict):
        accounts = {}
    current = accounts.get(canonical)
    row = dict(current) if isinstance(current, dict) else {}
    row["email"] = email.strip()
    accounts[canonical] = row
    taxonomy = document.get("taxonomy")
    lines: list[str] = []
    as_of_value = document.get("as_of")
    if isinstance(as_of_value, int) and not isinstance(as_of_value, bool) and as_of_value >= 0:
        lines.append(f"as_of = {as_of_value}")
        lines.append("")
    time_block = document.get("time")
    if isinstance(time_block, dict):
        lines.append("[time]")
        for key in ("stale_days", "old_days"):
            if key in time_block:
                lines.append(f"{key} = {_optional_days(time_block, key, 0)}")
        lines.append("")
    if isinstance(taxonomy, str) and taxonomy and '"' not in taxonomy:
        lines.append(f'taxonomy = "{taxonomy}"')
        lines.append("")
    configured_rules = document.get("rules")
    if isinstance(configured_rules, str) and configured_rules:
        lines.append(f'rules = "{_rules_value(configured_rules)}"')
        lines.append("")
    inference = document.get("inference")
    if isinstance(inference, dict):
        saved = [
            (key, value)
            for key, value in inference.items()
            if isinstance(key, str) and isinstance(value, bool)
        ]
        named = _embedder_value(inference["embedder"]) if "embedder" in inference else None
        counted = [
            (key, _required_count(inference[key], key))
            for key in ("batch_size", "max_tokens")
            if key in inference
        ]
        device = _device_value(inference["device"]) if "device" in inference else None
        llm = inference.get("llm")
        if saved or named or counted or device:
            lines.append("[inference]")
            if named:
                lines.append(f'embedder = "{named}"')
            if device:
                lines.append(f'device = "{device}"')
            for key, value in sorted(saved):
                lines.append(f"{key} = {'true' if value else 'false'}")
            for key, value in counted:
                lines.append(f"{key} = {value}")
            lines.append("")
        if isinstance(llm, dict):
            lines.extend(_llm_lines(llm))
    review = document.get("review")
    if isinstance(review, dict) and review.get("allow_override") is True:
        lines.extend(["[review]", "allow_override = true", ""])
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


def _llm_lines(block: dict[str, object]) -> list[str]:
    lines = ["[inference.llm]"]
    for key in ("base_url", "model"):
        value = block.get(key)
        if not isinstance(value, str):
            continue
        if '"' in value or "\n" in value:
            raise UsageError("local llm config contains a character the config file cannot store")
        lines.append(f'{key} = "{value}"')
    if block.get("allow_remote") is True:
        lines.append("allow_remote = true")
    lines.append("")
    return lines


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
