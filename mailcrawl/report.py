"""Archive counts and dry-run counts. Dry-run does not write decisions."""

from __future__ import annotations

import datetime
import sqlite3
import time
from collections.abc import Iterable

from mailcrawl.contract import GMAIL_LABEL_SQL
from mailcrawl.rules import RulesConfig, bundled_rules_path, load_rules, preview_rules
from mailcrawl.taxonomy import (
    GMAIL_CATEGORIES,
    GMAIL_TABS,
    backfill_categories,
    inactive_content_rules,
    inactive_reason,
)


def stats(
    conn: sqlite3.Connection,
    *,
    categories: Iterable[str] | None = None,
) -> dict[str, object]:
    messages = int(conn.execute("SELECT count(*) FROM messages").fetchone()[0])
    threads = int(conn.execute("SELECT count(*) FROM threads").fetchone()[0])
    senders = int(conn.execute("SELECT count(*) FROM senders").fetchone()[0])
    by_month: dict[str, int] = {}
    for (stamp,) in conn.execute(
        "SELECT internal_date FROM messages WHERE internal_date IS NOT NULL"
    ):
        key = _month(int(stamp))
        if key is None:
            continue
        by_month[key] = by_month.get(key, 0) + 1
    backfill_categories(conn)
    active = _active(categories)
    return {
        "by_category": _category_counts(conn, active),
        "by_month": by_month,
        "messages": messages,
        "senders": senders,
        "threads": threads,
        "uncategorized": _uncategorized(conn),
    }


def dry_run(
    conn: sqlite3.Connection,
    *,
    categories: Iterable[str] | None = None,
    now: int | None = None,
    config: RulesConfig | None = None,
) -> dict[str, object]:
    backfill_categories(conn)
    active = _active(categories)
    if config is None:
        config = load_rules(bundled_rules_path())
    stamp_now = int(time.time()) if now is None else now
    by_state: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_label: dict[str, int] = {}
    by_sender: dict[str, int] = {}
    by_month: dict[str, int] = {}
    decisions_by_category: dict[str, dict[str, int]] = {}
    undecided = 0
    for preview in preview_rules(conn, now=stamp_now, config=config):
        state = preview.next_state
        rule_id = preview.rule_id
        by_state[state] = by_state.get(state, 0) + 1
        by_rule[rule_id] = by_rule.get(rule_id, 0) + 1
        if rule_id == "else":
            undecided += 1
        for key in _thread_categories(conn, preview.thread_pk, active):
            bucket = decisions_by_category.setdefault(key, {})
            bucket[rule_id] = bucket.get(rule_id, 0) + 1
        thread_pk = preview.thread_pk
        for (name,) in conn.execute(
            f"""
            SELECT DISTINCT labels.name
            FROM message_labels
            JOIN messages ON messages.msg_pk = message_labels.msg_pk
            JOIN labels
              ON labels.account_id = message_labels.account_id
             AND labels.gmail_label_id = message_labels.gmail_label_id
            WHERE messages.thread_pk = ? AND {GMAIL_LABEL_SQL}
            ORDER BY labels.name
            """,
            (thread_pk,),
        ):
            label = str(name)
            by_label[label] = by_label.get(label, 0) + 1
        sender = conn.execute(
            """
            SELECT sender_id FROM messages
            WHERE thread_pk = ? AND is_from_me = 0 AND sender_id IS NOT NULL
            ORDER BY internal_date DESC, msg_pk DESC
            LIMIT 1
            """,
            (thread_pk,),
        ).fetchone()
        if sender is not None:
            key = str(int(sender["sender_id"]))
            by_sender[key] = by_sender.get(key, 0) + 1
        stamp = conn.execute(
            """
            SELECT max(internal_date) FROM messages
            WHERE thread_pk = ? AND internal_date IS NOT NULL
            """,
            (thread_pk,),
        ).fetchone()[0]
        if stamp is not None:
            month = _month(int(stamp))
            if month is not None:
                by_month[month] = by_month.get(month, 0) + 1
    return {
        "by_category": _category_counts(conn, active),
        "by_label": by_label,
        "by_month": by_month,
        "by_rule": by_rule,
        "by_sender": by_sender,
        "by_state": by_state,
        "decisions_by_category": {
            name: dict(sorted(rules.items()))
            for name, rules in sorted(decisions_by_category.items())
        },
        "inactive_reason": inactive_reason(active),
        "inactive_rules": inactive_content_rules(active),
        "undecided": undecided,
    }


def _thread_categories(
    conn: sqlite3.Connection,
    thread_pk: int,
    categories: tuple[str, ...],
) -> list[str]:
    wanted = set(categories)
    rows = conn.execute(
        f"""
        SELECT DISTINCT labels.catalogue_key
        FROM messages
        JOIN message_labels ON message_labels.msg_pk = messages.msg_pk
        JOIN labels
          ON labels.account_id = message_labels.account_id
         AND labels.gmail_label_id = message_labels.gmail_label_id
         AND {GMAIL_LABEL_SQL}
        WHERE messages.thread_pk = ?
        """,
        (thread_pk,),
    ).fetchall()
    return sorted(str(key) for (key,) in rows if key in wanted)


def _uncategorized(conn: sqlite3.Connection) -> int:
    placeholders = ", ".join("?" for _ in GMAIL_TABS)
    row = conn.execute(
        f"""
        SELECT count(*) FROM messages
        WHERE NOT EXISTS (
          SELECT 1
          FROM message_labels
          JOIN labels
            ON labels.account_id = message_labels.account_id
           AND labels.gmail_label_id = message_labels.gmail_label_id
           AND {GMAIL_LABEL_SQL}
          WHERE message_labels.msg_pk = messages.msg_pk
            AND labels.catalogue_key IN ({placeholders})
        )
        """,
        GMAIL_TABS,
    ).fetchone()
    return int(row[0])


def _active(categories: Iterable[str] | None) -> tuple[str, ...]:
    if categories is None:
        return GMAIL_CATEGORIES
    return tuple(categories)


def _category_counts(conn: sqlite3.Connection, categories: tuple[str, ...]) -> dict[str, int]:
    counts = {name: 0 for name in categories}
    rows = conn.execute(
        f"""
        SELECT messages.msg_pk, labels.catalogue_key
        FROM messages
        LEFT JOIN message_labels ON message_labels.msg_pk = messages.msg_pk
        LEFT JOIN labels
          ON labels.account_id = message_labels.account_id
         AND labels.gmail_label_id = message_labels.gmail_label_id
         AND {GMAIL_LABEL_SQL}
        """
    ).fetchall()
    by_message: dict[int, set[str]] = {}
    for msg_pk, key in rows:
        found = by_message.setdefault(int(msg_pk), set())
        if key in counts:
            found.add(str(key))
    for found in by_message.values():
        for name in found:
            counts[name] += 1
    return counts


def _month(stamp: int) -> str | None:
    if stamp < 0:
        return None
    moment = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
    return moment.strftime("%Y-%m")
