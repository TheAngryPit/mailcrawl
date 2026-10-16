"""Store one current decision per thread and do not infer it again."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

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


def decide_once(
    conn: sqlite3.Connection,
    thread_pk: int,
    now: int,
    produce: Callable[[], DecisionDraft],
) -> sqlite3.Row:
    stored = current_decision(conn, thread_pk)
    if stored is not None:
        return stored
    draft = produce()
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
    conn.commit()
    stored = current_decision(conn, thread_pk)
    if stored is None:
        raise RuntimeError("decision was not stored")
    return stored
