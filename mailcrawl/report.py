# SPDX-License-Identifier: MIT
"""Archive counts and dry-run counts. Dry-run does not write decisions."""

from __future__ import annotations

import csv
import datetime
import random
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from mailcrawl.advise import MODEL_NAME, AdviceCase, Advisor
from mailcrawl.clock import as_of
from mailcrawl.contract import GMAIL_LABEL_SQL
from mailcrawl.reevaluate import revive_kind
from mailcrawl.rules import RulesConfig, bundled_rules_path, load_rules, preview_rules
from mailcrawl.timefacts import thread_time_facts
from mailcrawl.taxonomy import (
    GMAIL_CATEGORIES,
    GMAIL_TABS,
    backfill_categories,
    effective_keys,
    inactive_content_rules,
    inactive_reason,
)


def stats(
    conn: sqlite3.Connection,
    *,
    categories: Iterable[str] | None = None,
    label_keys: dict[str, tuple[str, ...]] | None = None,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
    security_senders: frozenset[str] = frozenset(),
    test_recipients: frozenset[str] = frozenset(),
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
        test_recipients=test_recipients,
    )
    return {
        "by_category": _category_counts(conn, active),
        "by_month": by_month,
        "messages": messages,
        "needs_review_else": fallthrough,
        "needs_review_rule0": rule0,
        "revive_forgotten": _revive_count(conn, "forgotten"),
        "revive_opportunity": _revive_count(conn, "opportunity"),
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
    label_keys: dict[str, tuple[str, ...]] | None = None,
    export_path: Path | None = None,
    sample: int | None = None,
    seed: int = 0,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
    security_senders: frozenset[str] = frozenset(),
    test_recipients: frozenset[str] = frozenset(),
    advisor: Advisor | None = None,
    policy=None,
    review: bool = False,
) -> dict[str, object]:
    backfill_categories(conn)
    active = _active(categories)
    if config is None:
        config = load_rules(bundled_rules_path())
    stamp_now = as_of(now)
    by_state: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_label: dict[str, int] = {}
    by_sender: dict[str, int] = {}
    by_month: dict[str, int] = {}
    decisions_by_category: dict[str, dict[str, int]] = {}
    export_rows: list[_ExportRow] = []
    undecided = 0
    for preview in preview_rules(
        conn,
        now=stamp_now,
        config=config,
        label_keys=label_keys,
        owners=owners,
        domains=domains,
        security_senders=security_senders,
        test_recipients=test_recipients,
        policy=policy,
        review=review,
    ):
        state = preview.next_state
        rule_id = preview.rule_id
        reported = _reported_state(state, rule_id)
        by_state[reported] = by_state.get(reported, 0) + 1
        by_rule[rule_id] = by_rule.get(rule_id, 0) + 1
        suggestion = ("", "", "")
        if rule_id == "else":
            undecided += 1
            if advisor is not None:
                advice = advisor.advise(
                    AdviceCase(preview.thread_pk, rule_id, preview.lane)
                )
                suggestion = (advice.rule_id, advice.lane, advice.confidence)
        if export_path is not None:
            export_rows.append(
                _export_row(
                    conn,
                    preview.thread_pk,
                    rule_id,
                    preview.lane,
                    preview.next_state,
                    preview.time_reason,
                    stamp_now,
                    label_keys,
                    suggestion,
                )
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
    if advisor is not None:
        counted["advice"] = {
            "applied": False,
            "else_threads": undecided,
            "model": MODEL_NAME,
        }
    if label_keys is not None:
        counted["override_matches"] = _override_matches(conn, label_keys)
    if export_path is not None:
        chosen = export_rows if sample is None else _stratified(export_rows, sample, seed)
        counted["exported"] = _write_decisions(export_path, chosen)
    return counted


class _ExportRow(NamedTuple):
    gmail_thread_id: str
    rule: str
    lane: str
    override_match: str
    suggested_rule: str
    suggested_lane: str
    suggested_confidence: str
    state: str
    last_human: str
    age_days: str
    date_trusted: str
    past_deadline: str
    reason: str


def _export_row(
    conn: sqlite3.Connection,
    thread_pk: int,
    rule_id: str,
    lane: str,
    state: str,
    reason: str,
    moment: int,
    label_keys: dict[str, tuple[str, ...]] | None,
    suggestion: tuple[str, str, str],
) -> _ExportRow:
    thread_id = conn.execute(
        "SELECT gmail_thread_id FROM threads WHERE thread_pk = ?",
        (thread_pk,),
    ).fetchone()
    facts = thread_time_facts(conn, thread_pk, moment)
    return _ExportRow(
        str(thread_id[0]),
        rule_id,
        lane,
        _override_cell(conn, thread_pk, label_keys),
        suggestion[0],
        suggestion[1],
        suggestion[2],
        state,
        facts.last_human,
        "" if facts.age_days is None else str(facts.age_days),
        "1" if facts.date_trusted else "0",
        "1" if facts.past_deadline else "0",
        reason,
    )


def _override_cell(
    conn: sqlite3.Connection,
    thread_pk: int,
    label_keys: dict[str, tuple[str, ...]] | None,
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
    found: set[str] = set()
    for (name,) in rows:
        found.update(label_keys.get(str(name), ()))
    return "|".join(sorted(found))


def _stratified(
    rows: list[_ExportRow],
    count: int,
    seed: int,
) -> list[_ExportRow]:
    grouped: dict[str, list[_ExportRow]] = {}
    for row in rows:
        grouped.setdefault(row.rule, []).append(row)
    rng = random.Random(seed)
    chosen: list[_ExportRow] = []
    rest: list[_ExportRow] = []
    for rule in sorted(grouped):
        bucket = list(grouped[rule])
        rng.shuffle(bucket)
        chosen.append(bucket[0])
        rest.extend(bucket[1:])
    if len(chosen) < count:
        rng.shuffle(rest)
        chosen.extend(rest[: count - len(chosen)])
    return chosen


def _write_decisions(path: Path, rows: list[_ExportRow]) -> int:
    ordered = sorted(rows, key=lambda row: row.gmail_thread_id)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "gmail_thread_id",
                "rule",
                "lane",
                "override_match",
                "suggested_rule",
                "suggested_lane",
                "suggested_confidence",
                "state",
                "last_human",
                "age_days",
                "date_trusted",
                "past_deadline",
                "reason",
            ]
        )
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
    label_keys: dict[str, tuple[str, ...]] | None = None,
    owners: frozenset[str] = frozenset(),
    domains: frozenset[str] = frozenset(),
    security_senders: frozenset[str] = frozenset(),
    test_recipients: frozenset[str] = frozenset(),
) -> tuple[int, int]:
    if config is None:
        config = load_rules(bundled_rules_path())
    stamp_now = as_of(now)
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
        test_recipients=test_recipients,
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
    label_keys: dict[str, tuple[str, ...]] | None = None,
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
        for key in effective_keys(str(name), None if stored is None else str(stored), label_keys):
            if key in wanted:
                found.add(key)
    return sorted(found)


def _override_matches(
    conn: sqlite3.Connection,
    label_keys: dict[str, tuple[str, ...]],
) -> dict[str, dict[str, int]]:
    categories = sorted({category for names in label_keys.values() for category in names})
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
        for category in label_keys.get(str(name), ()):
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


def _revive_count(conn: sqlite3.Connection, kind: str) -> int:
    count = 0
    for (evidence,) in conn.execute(
        "SELECT evidence FROM decisions WHERE superseded_by IS NULL"
    ):
        if revive_kind(evidence) == kind:
            count += 1
    return count


def _month(stamp: int) -> str | None:
    if stamp < 0:
        return None
    moment = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
    return moment.strftime("%Y-%m")
