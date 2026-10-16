"""Keyword search over one account database."""

from __future__ import annotations

import sqlite3

from mailcrawl.contract import GMAIL_LABEL_SQL
from mailcrawl.paths import UsageError

_FTS_WEIGHTS = (10.0, 1.0, 10.0, 1.0)


def search_messages(
    conn: sqlite3.Connection,
    query: str,
    *,
    sender: str | None = None,
    label: str | None = None,
    mime: str | None = None,
    image_only: int | None = None,
    since: int | None = None,
    until: int | None = None,
) -> list[dict[str, str | None]]:
    text = query.strip()
    if not text:
        raise UsageError("search needs a query")
    candidates = _candidates(conn, sender, label, mime, image_only, since, until)
    if candidates is not None and not candidates:
        return []
    fts_ids = _match(conn, "msg_fts", text, candidates, ranked=True)
    seen = set(fts_ids)
    trigram_ids = []
    if len(text) >= 3:
        for msg_pk in _match(conn, "msg_trigram", text, candidates, ranked=False):
            if msg_pk not in seen:
                trigram_ids.append(msg_pk)
                seen.add(msg_pk)
    hits: list[dict[str, str | None]] = []
    for msg_pk, source in [(pk, "fts") for pk in fts_ids] + [(pk, "trigram") for pk in trigram_ids]:
        message = conn.execute(
            """
            SELECT gmail_message_id, subject, body_text
            FROM messages WHERE msg_pk = ?
            """,
            (msg_pk,),
        ).fetchone()
        if message is None:
            continue
        hits.append(
            {
                "gmail_message_id": message["gmail_message_id"],
                "snippet": _snippet(message["subject"], message["body_text"]),
                "source": source,
                "subject": message["subject"],
            }
        )
    return hits


def _candidates(
    conn: sqlite3.Connection,
    sender: str | None,
    label: str | None,
    mime: str | None,
    image_only: int | None,
    since: int | None,
    until: int | None,
) -> set[int] | None:
    clauses: list[str] = []
    params: list[object] = []
    if sender:
        clauses.append("messages.from_hdr LIKE ? ESCAPE '\\'")
        params.append(f"%{_like(sender)}%")
    if since is not None:
        clauses.append("messages.internal_date >= ?")
        params.append(since)
    if until is not None:
        clauses.append("messages.internal_date <= ?")
        params.append(until)
    if label:
        clauses.append(
            f"""
            EXISTS (
              SELECT 1 FROM message_labels
              JOIN labels
                ON labels.account_id = message_labels.account_id
               AND labels.gmail_label_id = message_labels.gmail_label_id
              WHERE message_labels.msg_pk = messages.msg_pk
                AND (labels.name = ? OR message_labels.gmail_label_id = ?)
                AND {GMAIL_LABEL_SQL}
            )
            """
        )
        params.extend((label, label))
    if mime:
        clauses.append(
            """
            EXISTS (
              SELECT 1 FROM attachments
              WHERE attachments.msg_pk = messages.msg_pk
                AND (
                  lower(COALESCE(attachments.mime_type, '')) = lower(?)
                  OR lower(COALESCE(attachments.mime_type, '')) LIKE lower(?) || ';%'
                )
            )
            """
        )
        params.extend((mime, mime))
    if image_only is not None:
        clauses.append(
            """
            EXISTS (
              SELECT 1 FROM threads
              WHERE threads.thread_pk = messages.thread_pk
                AND threads.image_only = ?
            )
            """
        )
        params.append(image_only)
    if not clauses:
        return None
    rows = conn.execute(
        "SELECT messages.msg_pk FROM messages WHERE " + " AND ".join(clauses),
        params,
    ).fetchall()
    return {int(row[0]) for row in rows}


def _match(
    conn: sqlite3.Connection,
    table: str,
    query: str,
    candidates: set[int] | None,
    *,
    ranked: bool,
) -> list[int]:
    where = f"{table} MATCH ?"
    params: list[object] = [query]
    if candidates is not None:
        marks = ",".join("?" for _ in candidates)
        where += f" AND rowid IN ({marks})"
        params.extend(sorted(candidates))
    order = ""
    if ranked:
        weights = ", ".join(str(weight) for weight in _FTS_WEIGHTS)
        order = f" ORDER BY bm25({table}, {weights})"
    try:
        rows = conn.execute(f"SELECT rowid FROM {table} WHERE {where}{order}", params).fetchall()
    except sqlite3.OperationalError as exc:
        raise UsageError(str(exc)) from exc
    return [int(row[0]) for row in rows]


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(subject: str | None, body: str | None) -> str:
    parts = [part.strip() for part in (subject, body) if part and part.strip()]
    return " ".join(parts)[:240]
