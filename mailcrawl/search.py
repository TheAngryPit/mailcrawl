"""FTS search over one account database."""

from __future__ import annotations

import sqlite3

from mailcrawl.paths import UsageError


def search_messages(conn: sqlite3.Connection, query: str) -> list[dict[str, str | None]]:
    text = query.strip()
    if not text:
        raise UsageError("search needs a query")
    try:
        rows = conn.execute(
            "SELECT rowid FROM msg_fts WHERE msg_fts MATCH ? ORDER BY rowid",
            (text,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise UsageError(str(exc)) from exc
    hits: list[dict[str, str | None]] = []
    for row in rows:
        message = conn.execute(
            """
            SELECT gmail_message_id, subject, body_text
            FROM messages WHERE msg_pk = ?
            """,
            (row[0],),
        ).fetchone()
        if message is None:
            continue
        hits.append(
            {
                "gmail_message_id": message["gmail_message_id"],
                "snippet": _snippet(message["subject"], message["body_text"]),
                "subject": message["subject"],
            }
        )
    return hits


def _snippet(subject: str | None, body: str | None) -> str:
    parts = [part.strip() for part in (subject, body) if part and part.strip()]
    return " ".join(parts)[:240]
