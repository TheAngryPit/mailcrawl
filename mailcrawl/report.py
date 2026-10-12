"""Archive counts and dry-run counts. Dry-run does not write decisions."""

from __future__ import annotations

import csv
import datetime
import random
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from mailcrawl.contract import GMAIL_LABEL_SQL
from mailcrawl.rules import RulesConfig, bundled_rules_path, load_rules, preview_rules
from mailcrawl.taxonomy import (
    GMAIL_CATEGORIES,
    GMAIL_TABS,
    backfill_categories,
    effective_key,
    inactive_content_rules,
    inactive_reason,
)


def stats(
    conn: sqlite3.Connection,
    *,
    categories: Iterable[str] | None = None,
    label_keys: dict[str, str] | None = None,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
    security_senders: frozenset[str] = frozenset(),
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
    rule0, fallthrough = _review_split(
        conn,
        now=None,
        config=None,
        label_keys=label_keys,
        owners=owners,
        domains=domains,
        security_senders=security_senders,
    )
    return {
        "by_category": _category_counts(conn, active),
        "by_month": by_month,
        "messages": messages,
        "needs_review_else": fallthrough,
        "needs_review_rule0": rule0,
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
    label_keys: dict[str, str] | None = None,
    export_path: Path | None = None,
    sample: int | None = None,
    seed: int = 0,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
    security_senders: frozenset[str] = frozenset(),
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
    export_rows: list[tuple[str, str, str, str]] = []
    undecided = 0
    for preview in preview_rules(
        conn,
        now=stamp_now,
        config=config,
        label_keys=label_keys,
        owners=owners,
        domains=domains,
        security_senders=security_senders,
    ):
        state = preview.next_state
        rule_id = preview.rule_id
        reported = _reported_state(state, rule_id)
        by_state[reported] = by_state.get(reported, 0) + 1
        by_rule[rule_id] = by_rule.get(rule_id, 0) + 1
        if rule_id == "else":
            undecided += 1
        if export_path is not None:
            export_rows.append(
                _export_row(conn, preview.thread_pk, rule_id, preview.lane, label_keys)
            )
        for key in _thread_categories(conn, preview.thread_pk, active, label_keys):
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
    counted = {
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
    if label_keys is not None:
        counted["override_matches"] = _override_matches(conn, label_keys)
    if export_path is not None:
        chosen = export_rows if sample is None else _stratified(export_rows, sample, seed)
        counted["exported"] = _write_decisions(export_path, chosen)
    return counted


def _export_row(
    conn: sqlite3.Connection,
    thread_pk: int,
    rule_id: str,
    lane: str,
    label_keys: dict[str, str] | None,
) -> tuple[str, str, str, str]:
    thread_id = conn.execute(
        "SELECT gmail_thread_id FROM threads WHERE thread_pk = ?",
        (thread_pk,),
    ).fetchone()
    return (str(thread_id[0]), rule_id, lane, _override_cell(conn, thread_pk, label_keys))


def _override_cell(
    conn: sqlite3.Connection,
    thread_pk: int,
    label_keys: dict[str, str] | None,
) -> str:
    if not label_keys:
        return ""
    rows = conn.execute(
        f"""
        SELECT DISTINCT labels.name
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
    found = sorted({label_keys[str(name)] for (name,) in rows if str(name) in label_keys})
    return "|".join(found)


def _stratified(
    rows: list[tuple[str, str, str, str]],
    count: int,
    seed: int,
) -> list[tuple[str, str, str, str]]:
    grouped: dict[str, list[tuple[str, str, str, str]]] = {}
    for row in rows:
        grouped.setdefault(row[1], []).append(row)
    rng = random.Random(seed)
    chosen: list[tuple[str, str, str, str]] = []
    rest: list[tuple[str, str, str, str]] = []
    for rule in sorted(grouped):
        bucket = list(grouped[rule])
        rng.shuffle(bucket)
        chosen.append(bucket[0])
        rest.extend(bucket[1:])
    if len(chosen) < count:
        rng.shuffle(rest)
        chosen.extend(rest[: count - len(chosen)])
    return chosen


def _write_decisions(path: Path, rows: list[tuple[str, str, str, str]]) -> int:
    ordered = sorted(rows, key=lambda row: row[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["gmail_thread_id", "rule", "lane", "override_match"])
        writer.writerows(ordered)
    return len(ordered)


def _reported_state(state: str, rule_id: str) -> str:
    if state == "needs_review" and rule_id == "0":
        return "needs_review_rule0"
    if state == "needs_review" and rule_id == "else":
        return "needs_review_else"
    return state


def _review_split(
    conn: sqlite3.Connection,
    *,
    now: int | None,
    config: RulesConfig | None,
    label_keys: dict[str, str] | None = None,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
    security_senders: frozenset[str] = frozenset(),
) -> tuple[int, int]:
    if config is None:
        config = load_rules(bundled_rules_path())
    stamp_now = int(time.time()) if now is None else now
    rule0 = 0
    fallthrough = 0
    for preview in preview_rules(
        conn,
        now=stamp_now,
        config=config,
        label_keys=label_keys,
        owners=owners,
        domains=domains,
        security_senders=security_senders,
    ):
        if preview.rule_id == "0" and preview.next_state == "needs_review":
            rule0 += 1
        elif preview.rule_id == "else" and preview.next_state == "needs_review":
            fallthrough += 1
    return rule0, fallthrough


def _thread_categories(
    conn: sqlite3.Connection,
    thread_pk: int,
    categories: tuple[str, ...],
    label_keys: dict[str, str] | None = None,
) -> list[str]:
    wanted = set(categories)
    rows = conn.execute(
        f"""
        SELECT DISTINCT labels.name, labels.catalogue_key
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
    found: set[str] = set()
    for name, stored in rows:
        key = effective_key(str(name), None if stored is None else str(stored), label_keys)
        if key in wanted:
            found.add(key)
    return sorted(found)


def _override_matches(
    conn: sqlite3.Connection,
    label_keys: dict[str, str],
) -> dict[str, dict[str, int]]:
    categories = sorted(set(label_keys.values()))
    messages: dict[str, set[int]] = {category: set() for category in categories}
    threads: dict[str, set[int]] = {category: set() for category in categories}
    rows = conn.execute(
        f"""
        SELECT messages.msg_pk, messages.thread_pk, labels.name
        FROM message_labels
        JOIN messages ON messages.msg_pk = message_labels.msg_pk
        JOIN labels
          ON labels.account_id = message_labels.account_id
         AND labels.gmail_label_id = message_labels.gmail_label_id
         AND {GMAIL_LABEL_SQL}
        """
    ).fetchall()
    for msg_pk, thread_pk, name in rows:
        category = label_keys.get(str(name))
        if category is None:
            continue
        messages[category].add(int(msg_pk))
        threads[category].add(int(thread_pk))
    return {
        category: {"messages": len(messages[category]), "threads": len(threads[category])}
        for category in categories
    }


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
