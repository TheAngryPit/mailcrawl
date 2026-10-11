"""Read-only archive counts and dry-run counts."""

from __future__ import annotations

import datetime
import sqlite3

from mailcrawl.contract import GMAIL_LABEL_SQL
from mailcrawl.taxonomy import CATEGORIES, empty_no_override, header_is_automated


def stats(conn: sqlite3.Connection, *, has_override: bool = False) -> dict[str, object]:
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
    return {
        "by_category": _category_counts(conn),
        "by_month": by_month,
        "empty_no_override": empty_no_override(has_override),
        "messages": messages,
        "senders": senders,
        "threads": threads,
    }


def dry_run(conn: sqlite3.Connection, *, has_override: bool = False) -> dict[str, object]:
    by_state: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_label: dict[str, int] = {}
    by_sender: dict[str, int] = {}
    by_month: dict[str, int] = {}
    rows = conn.execute(
        """
        SELECT decision_id, thread_pk, next_state, rule_id
        FROM decisions
        WHERE superseded_by IS NULL
        ORDER BY thread_pk
        """
    ).fetchall()
    for row in rows:
        state = str(row["next_state"])
        rule_id = str(row["rule_id"])
        by_state[state] = by_state.get(state, 0) + 1
        by_rule[rule_id] = by_rule.get(rule_id, 0) + 1
        thread_pk = int(row["thread_pk"])
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
        "by_category": _category_counts(conn),
        "by_label": by_label,
        "by_month": by_month,
        "by_rule": by_rule,
        "by_sender": by_sender,
        "by_state": by_state,
        "empty_no_override": empty_no_override(has_override),
    }


def _category_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {name: 0 for name in CATEGORIES}
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
    for msg_pk, name, value in conn.execute(
        """
        SELECT msg_pk, name, value FROM headers
        WHERE name IN ('list-unsubscribe', 'list-id', 'precedence')
        """
    ):
        if header_is_automated(((str(name), str(value)),)):
            by_message.setdefault(int(msg_pk), set()).add("automated")
    for found in by_message.values():
        for name in found:
            counts[name] += 1
    return counts


def _month(stamp: int) -> str | None:
    if stamp < 0:
        return None
    moment = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
    return moment.strftime("%Y-%m")
