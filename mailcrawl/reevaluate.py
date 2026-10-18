"""Revisit stored rule, kNN, and model rows with time facts. Human rows stay."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from mailcrawl.decisions import DecisionDraft, insert_draft
from mailcrawl.rules import _time_reason, supersede_current
from mailcrawl.timefacts import TimePolicy, apply_time, thread_time_facts

TIME_DETAIL = "time"


def reevaluate_time(
    conn: sqlite3.Connection,
    *,
    now: int,
    policy: TimePolicy | None = None,
    apply: bool,
) -> list[dict[str, str]]:
    """Apply time facts to current rule, knn, and model rows. Human and needs_review stay."""
    rows: list[dict[str, str]] = []
    threads = conn.execute(
        "SELECT thread_pk, gmail_thread_id FROM threads ORDER BY thread_pk"
    ).fetchall()
    for thread in threads:
        thread_pk = int(thread[0])
        current = conn.execute(
            """
            SELECT * FROM decisions
            WHERE thread_pk = ? AND superseded_by IS NULL
            """,
            (thread_pk,),
        ).fetchone()
        if current is None:
            continue
        origin = str(current["origin"])
        state = str(current["next_state"])
        lane = str(current["lane"])
        record = {
            "changed": "0",
            "gmail_thread_id": str(thread[1]),
            "lane": lane,
            "next_lane": lane,
            "next_state": state,
            "origin": origin,
            "reason": "",
            "skipped": "",
            "state": state,
        }
        if origin == "human":
            record["skipped"] = "human"
            rows.append(record)
            continue
        if state == "needs_review":
            record["skipped"] = "needs_review"
            rows.append(record)
            continue
        if origin not in ("rule", "knn", "model"):
            record["skipped"] = "origin"
            rows.append(record)
            continue
        evidence = _stored_evidence(current["evidence"])
        review = current["review_date"]
        draft = DecisionDraft(
            next_state=state,
            lane=lane,
            rule_id="" if current["rule_id"] is None else str(current["rule_id"]),
            rules_version=str(current["rules_version"]),
            origin=origin,
            source_detail=str(current["source_detail"]),
            evidence=evidence,
            priority=None if current["priority"] is None else int(current["priority"]),
            review_date=None if review is None else int(review),
            muted=int(current["muted"]),
        )
        adjusted = apply_time(draft, thread_time_facts(conn, thread_pk, now), policy)
        if adjusted is draft:
            rows.append(record)
            continue
        record["next_state"] = adjusted.next_state
        record["next_lane"] = adjusted.lane
        record["reason"] = _time_reason(adjusted.evidence)
        same = (
            adjusted.next_state == state
            and adjusted.lane == lane
            and adjusted.review_date == draft.review_date
        )
        if same:
            rows.append(record)
            continue
        record["changed"] = "1"
        if apply:
            _supersede_time(conn, current, adjusted, now)
        rows.append(record)
    if apply:
        conn.commit()
    return rows


def _stored_evidence(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, str) or not value:
        return ()
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed if isinstance(item, dict))


def _supersede_time(conn: sqlite3.Connection, current: sqlite3.Row, draft: DecisionDraft, now: int) -> None:
    review_date = draft.review_date
    if draft.next_state == "waiting" and review_date is None:
        review_date = now
    rule_id = "time" if draft.rule_id == "harness" else draft.rule_id
    stored = replace(draft, source_detail=TIME_DETAIL, review_date=review_date, rule_id=rule_id)
    supersede_current(
        conn,
        int(current["decision_id"]),
        lambda: insert_draft(conn, int(current["thread_pk"]), stored, now),
    )
