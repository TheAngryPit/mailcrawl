"""Revisit stored rule, kNN, and model rows with time facts. Human rows stay."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from mailcrawl.decisions import DecisionDraft, insert_draft
from mailcrawl.rules import _time_reason, supersede_current
from mailcrawl.timefacts import TimePolicy, apply_time, present_time_closure, thread_time_facts

TIME_DETAIL = "time"


def reevaluate_time(
    conn: sqlite3.Connection,
    *,
    now: int,
    policy: TimePolicy | None = None,
    apply: bool,
    review: bool = False,
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
            "kind": revive_kind(current["evidence"]),
            "reason": "",
            "skipped": "",
            "state": state,
        }
        if origin == "human":
            record["skipped"] = "human"
            rows.append(record)
            continue
        if revive_holds(conn, current):
            record["skipped"] = "revive"
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
        stored_review = current["review_date"]
        draft = DecisionDraft(
            next_state=state,
            lane=lane,
            rule_id="" if current["rule_id"] is None else str(current["rule_id"]),
            rules_version=str(current["rules_version"]),
            origin=origin,
            source_detail=str(current["source_detail"]),
            evidence=evidence,
            priority=None if current["priority"] is None else int(current["priority"]),
            review_date=None if stored_review is None else int(stored_review),
            muted=int(current["muted"]),
        )
        adjusted = apply_time(draft, thread_time_facts(conn, thread_pk, now), policy)
        if adjusted is draft:
            rows.append(record)
            continue
        shown = present_time_closure(adjusted, lane, review)
        record["next_state"] = shown.next_state
        record["next_lane"] = shown.lane
        record["reason"] = _time_reason(adjusted.evidence)
        same = (
            shown.next_state == state
            and shown.lane == lane
            and shown.review_date == draft.review_date
        )
        if same:
            rows.append(record)
            continue
        record["changed"] = "1"
        if apply:
            _supersede_time(conn, current, shown, now)
        rows.append(record)
    if apply:
        conn.commit()
    return rows


def revive_kind(value: object) -> str:
    for item in _stored_evidence(value):
        kind = item.get("kind")
        if item.get("type") == "revive" and kind in ("forgotten", "opportunity"):
            return str(kind)
    return ""


def harness_revive(row: sqlite3.Row) -> bool:
    return str(row["rule_id"]) == "harness" and revive_kind(row["evidence"]) != ""


def message_after(conn: sqlite3.Connection, thread_pk: int, created_at: int) -> bool:
    found = conn.execute(
        """
        SELECT 1 FROM messages
        WHERE thread_pk = ?
          AND (
            (coalesce(date_trusted, 1) != 0 AND internal_date > ?)
            OR (coalesce(date_trusted, 1) = 0 AND imported_at > ?)
          )
        LIMIT 1
        """,
        (thread_pk, created_at, created_at),
    ).fetchone()
    return found is not None


def revive_holds(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    if not harness_revive(row):
        return False
    return not message_after(conn, int(row["thread_pk"]), int(row["created_at"]))


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
