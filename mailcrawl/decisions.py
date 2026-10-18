# SPDX-License-Identifier: MIT
"""Store one current decision per thread and do not infer it again."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

from mailcrawl.rules import supersede_current

ORIGINS = ("rule", "knn", "model", "human")
_SOURCE_FOR_ORIGIN = {
    "rule": "rule",
    "knn": "embedding-cluster",
    "model": "llm",
    "human": "human",
}


@dataclass(frozen=True)
class DecisionDraft:
    next_state: str
    lane: str
    rule_id: str
    rules_version: str
    origin: str
    source_detail: str
    evidence: tuple[dict[str, object], ...] = ()
    priority: int | None = None
    review_date: int | None = None
    muted: int = 0


def current_decision(conn: sqlite3.Connection, thread_pk: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM decisions
        WHERE thread_pk = ? AND superseded_by IS NULL
        """,
        (thread_pk,),
    ).fetchone()


def open_for_model(row: sqlite3.Row | None) -> bool:
    if row is None:
        return True
    return str(row["origin"]) == "rule" and str(row["next_state"]) == "needs_review"


def eligible_thread_pks(conn: sqlite3.Connection, *, include_image_only: bool = True) -> list[int]:
    image_sql = "" if include_image_only else "AND image_only = 0"
    rows = conn.execute(
        f"""
        SELECT thread_pk FROM threads
        WHERE (
          NOT EXISTS (
            SELECT 1 FROM decisions
            WHERE decisions.thread_pk = threads.thread_pk
              AND decisions.superseded_by IS NULL
          )
          OR EXISTS (
            SELECT 1 FROM decisions
            WHERE decisions.thread_pk = threads.thread_pk
              AND decisions.superseded_by IS NULL
              AND decisions.origin = 'rule'
              AND decisions.next_state = 'needs_review'
          )
        )
        {image_sql}
        ORDER BY thread_pk
        """
    ).fetchall()
    return [int(row[0]) for row in rows]


def decide_once(
    conn: sqlite3.Connection,
    thread_pk: int,
    now: int,
    produce: Callable[[], DecisionDraft],
) -> sqlite3.Row:
    stored = current_decision(conn, thread_pk)
    if stored is not None:
        return stored
    insert_draft(conn, thread_pk, produce(), now)
    conn.commit()
    return _stored(conn, thread_pk)


def decide_open(
    conn: sqlite3.Connection,
    thread_pk: int,
    now: int,
    produce: Callable[[], DecisionDraft | None],
) -> sqlite3.Row | None:
    stored = current_decision(conn, thread_pk)
    if not open_for_model(stored):
        return stored
    draft = produce()
    if draft is None:
        return stored
    if stored is None:
        insert_draft(conn, thread_pk, draft, now)
    else:
        supersede_current(
            conn,
            int(stored["decision_id"]),
            lambda: insert_draft(conn, thread_pk, draft, now),
        )
    conn.commit()
    return _stored(conn, thread_pk)


def _stored(conn: sqlite3.Connection, thread_pk: int) -> sqlite3.Row:
    stored = current_decision(conn, thread_pk)
    if stored is None:
        raise RuntimeError("decision was not stored")
    return stored


def insert_draft(
    conn: sqlite3.Connection,
    thread_pk: int,
    draft: DecisionDraft,
    now: int,
) -> int:
    if draft.origin not in ORIGINS:
        raise ValueError(draft.origin)
    evidence = json.dumps(list(draft.evidence), sort_keys=True, separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO decisions(
          thread_pk, next_state, priority, review_date, lane, muted,
          source, origin, source_detail, rule_id, rules_version, evidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thread_pk,
            draft.next_state,
            draft.priority,
            draft.review_date,
            draft.lane,
            draft.muted,
            _SOURCE_FOR_ORIGIN[draft.origin],
            draft.origin,
            draft.source_detail,
            draft.rule_id,
            draft.rules_version,
            evidence,
            now,
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
