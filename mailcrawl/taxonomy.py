"""Default taxonomy and the local per-account override."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from mailcrawl.configfile import taxonomy_path
from mailcrawl.paths import UsageError

GMAIL_SYSTEM = (
    "INBOX",
    "SENT",
    "STARRED",
    "IMPORTANT",
    "UNREAD",
    "DRAFT",
    "SPAM",
    "TRASH",
    "CHAT",
)
GMAIL_TABS = (
    "CATEGORY_PERSONAL",
    "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
)
GMAIL_CATEGORIES = GMAIL_SYSTEM + GMAIL_TABS
GMAIL_SET = frozenset(GMAIL_CATEGORIES)

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

_ACCOUNTS = frozenset({"thehive", "vcl", "personal"})
_META = frozenset({"synthetic", "note"})


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
    if category in GMAIL_SET:
        return "gmail"
    if category in ORIGIN:
        return "origin"
    return "content type"


def generic_category(gmail_label_id: str) -> str | None:
    if gmail_label_id in GMAIL_SET:
        return gmail_label_id
    return None


def active_categories(taxonomy: Taxonomy | None, alias: str) -> tuple[str, ...]:
    if taxonomy is None:
        return GMAIL_CATEGORIES
    found = list(GMAIL_CATEGORIES)
    seen = set(found)
    for name in taxonomy.names_for(alias).values():
        if name not in seen:
            found.append(name)
            seen.add(name)
    return tuple(found)


def inactive_content_rules(categories: Iterable[str]) -> list[str]:
    present = set(categories)
    inactive: list[str] = []
    content = bool({"newsletter", "job_alert", "notification"} & present) and "automated" in present
    if not content:
        inactive.append("2")
    if "newsletter" not in present:
        inactive.append("2-read")
    if not ({"invoice_receipt", "purchase_delivery"} & present):
        inactive.append("3")
    if "account_security" not in present:
        inactive.append("4")
    return inactive


def inactive_reason(categories: Iterable[str]) -> str | None:
    present = set(categories)
    if not inactive_content_rules(present):
        return None
    if present <= GMAIL_SET:
        return "gmail_default"
    return "schema"


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


def effective_key(
    name: str,
    stored: str | None,
    label_keys: dict[str, str] | None,
) -> str | None:
    """A taxonomy file maps label names. That map wins over a stored key for this read."""
    if label_keys is not None and name in label_keys:
        return label_keys[name]
    if stored:
        return stored
    return None


def missing_labels(taxonomy: Taxonomy, alias: str, present: set[str]) -> list[str]:
    return sorted(name for name in taxonomy.names_for(alias) if name not in present)


def present_label_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM labels WHERE gmail_label_id NOT LIKE 'pseudo:%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def backfill_categories(conn: sqlite3.Connection) -> int:
    """Fill missing Gmail category keys from stored label ids. Leave every other row alone."""
    placeholders = ", ".join("?" for _ in GMAIL_CATEGORIES)
    changed = conn.execute(
        f"""
        UPDATE labels
        SET catalogue_key = gmail_label_id,
            axis = 'gmail',
            in_catalogue = 1
        WHERE gmail_label_id IN ({placeholders})
          AND (catalogue_key IS NULL OR catalogue_key = '')
        """,
        GMAIL_CATEGORIES,
    ).rowcount
    conn.commit()
    return int(changed)


def _existing(path: Path, shown: str) -> Path:
    if not path.is_file():
        raise UsageError(f"taxonomy file not found: {shown}")
    return path


def _categories(table: dict[str, object]) -> dict[str, tuple[str, ...]]:
    mapped: dict[str, tuple[str, ...]] = {}
    for category, names in table.items():
        if not isinstance(category, str) or not category.strip():
            raise UsageError("taxonomy category needs a name")
        if not isinstance(names, list):
            raise UsageError(f"taxonomy category {category} needs a list of label names")
        clean: list[str] = []
        for name in names:
            if not isinstance(name, str) or not name.strip():
                raise UsageError(f"taxonomy category {category} needs a list of label names")
            clean.append(name.strip())
        mapped[category] = tuple(clean)
    return mapped
