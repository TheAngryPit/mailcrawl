"""Default taxonomy and the local per-account override."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from mailcrawl.configfile import taxonomy_path
from mailcrawl.paths import UsageError

CONTENT = (
    "newsletter",
    "job_alert",
    "notification",
    "invoice_receipt",
    "purchase_delivery",
    "account_security",
    "contract",
    "correspondence",
)
ORIGIN = ("human", "automated")
CATEGORIES = CONTENT + ORIGIN
CATEGORY_SET = frozenset(CATEGORIES)

GENERIC_LABELS = {
    "CATEGORY_PROMOTIONS": "newsletter",
    "CATEGORY_UPDATES": "notification",
    "CATEGORY_PERSONAL": "correspondence",
}
NO_GENERIC_SIGNAL = tuple(
    name for name in CATEGORIES if name not in set(GENERIC_LABELS.values()) and name != "automated"
)

_ACCOUNTS = frozenset({"thehive", "vcl", "personal"})
_META = frozenset({"synthetic", "note"})
_AUTO_HEADERS = frozenset({"list-unsubscribe", "list-id"})


@dataclass(frozen=True)
class Taxonomy:
    sections: dict[str, dict[str, tuple[str, ...]]]

    def names_for(self, alias: str) -> dict[str, str]:
        lists: dict[str, tuple[str, ...]] = {}
        for source in ("default", alias):
            for category, names in self.sections.get(source, {}).items():
                lists[category] = names
        by_name: dict[str, str] = {}
        for category, names in lists.items():
            for name in names:
                other = by_name.get(name)
                if other is not None and other != category:
                    raise UsageError(
                        f"taxonomy label {name} is in more than one category"
                    )
                by_name[name] = category
        return by_name

    def catalogue(self, alias: str) -> dict[str, dict[str, str]]:
        return {
            name: {"key": category, "axis": axis_of(category)}
            for name, category in self.names_for(alias).items()
        }


def axis_of(category: str) -> str:
    if category in ORIGIN:
        return "origin"
    return "content type"


def generic_category(gmail_label_id: str) -> str | None:
    return GENERIC_LABELS.get(gmail_label_id)


def header_is_automated(headers: Iterable[tuple[str, str]]) -> bool:
    for name, value in headers:
        key = str(name).strip().lower()
        if key in _AUTO_HEADERS:
            return True
        if key == "precedence" and re.search(r"(?<!\w)bulk(?!\w)", str(value).strip().lower()):
            return True
    return False


def empty_no_override(has_override: bool) -> list[str]:
    if has_override:
        return []
    return sorted(NO_GENERIC_SIGNAL)


def resolve_taxonomy(flag: str | None, config: Path) -> Path | None:
    if flag:
        return _existing(Path(flag), flag)
    env = os.environ.get("MAILCRAWL_TAXONOMY")
    if env:
        return _existing(Path(env), env)
    configured = taxonomy_path(config)
    if configured is not None:
        return _existing(configured, str(configured))
    home = Path.home() / ".config" / "mailcrawl" / "taxonomy.json"
    if home.is_file():
        return home
    return None


def load_taxonomy(path: Path) -> Taxonomy:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError("taxonomy file is not valid JSON") from exc
    if not isinstance(document, dict):
        raise UsageError("taxonomy file must be a JSON object")
    sections: dict[str, dict[str, tuple[str, ...]]] = {}
    for key, value in document.items():
        if key in _META:
            continue
        canon = "personal" if key == "pessoal" else key
        if canon != "default" and canon not in _ACCOUNTS:
            raise UsageError(f"unknown taxonomy account {key}")
        if canon in sections:
            raise UsageError(f"taxonomy account {canon} is listed twice")
        if not isinstance(value, dict):
            raise UsageError(f"taxonomy account {canon} must be a category map")
        sections[canon] = _categories(value)
    loaded = Taxonomy(sections)
    for alias in sorted(_ACCOUNTS):
        loaded.names_for(alias)
    return loaded


def missing_labels(taxonomy: Taxonomy, alias: str, present: set[str]) -> list[str]:
    return sorted(name for name in taxonomy.names_for(alias) if name not in present)


def present_label_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM labels WHERE gmail_label_id NOT LIKE 'pseudo:%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _existing(path: Path, shown: str) -> Path:
    if not path.is_file():
        raise UsageError(f"taxonomy file not found: {shown}")
    return path


def _categories(table: dict[str, object]) -> dict[str, tuple[str, ...]]:
    mapped: dict[str, tuple[str, ...]] = {}
    for category, names in table.items():
        if category not in CATEGORY_SET:
            raise UsageError(f"unknown taxonomy category {category}")
        if not isinstance(names, list):
            raise UsageError(f"taxonomy category {category} needs a list of label names")
        clean: list[str] = []
        for name in names:
            if not isinstance(name, str) or not name.strip():
                raise UsageError(f"taxonomy category {category} needs a list of label names")
            clean.append(name.strip())
        mapped[category] = tuple(clean)
    return mapped
