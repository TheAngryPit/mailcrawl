"""Open one account database and migrate it to schema v2."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

from mailcrawl.paths import UsageError, canonical_alias
from mailcrawl.schema import FTS_SQL, SCHEMA_SQL, USER_VERSION, V2_TABLES_SQL
from mailcrawl.textnorm import clean_body

DUMP_TABLES = (
    "accounts",
    "labels",
    "senders",
    "threads",
    "messages",
    "headers",
    "message_labels",
    "attachments",
    "cursors",
)


class UnsupportedSchema(Exception):
    """The database user_version is not 0, 1, or 2."""


def migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version == USER_VERSION:
        return
    if version == 0:
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {USER_VERSION}")
        conn.commit()
        return
    if version == 1:
        _migrate_v1(conn)
        return
    raise UnsupportedSchema(f"user_version {version}")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "body_text_raw" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN body_text_raw TEXT")
        conn.execute("UPDATE messages SET body_text_raw = body_text")
        rows = conn.execute("SELECT msg_pk, body_text FROM messages").fetchall()
        for msg_pk, body in rows:
            conn.execute(
                "UPDATE messages SET body_text = ? WHERE msg_pk = ?",
                (clean_body(body), msg_pk),
            )
    _rebuild_fts(conn)
    _execute_statements(conn, V2_TABLES_SQL)
    conn.execute(f"PRAGMA user_version = {USER_VERSION}")
    conn.commit()


def _execute_statements(conn: sqlite3.Connection, script: str) -> None:
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ""
    if pending.strip():
        conn.execute(pending)


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS msg_fts")
    _execute_statements(conn, FTS_SQL)
    messages = conn.execute(
        "SELECT msg_pk, subject, body_text, from_hdr FROM messages"
    ).fetchall()
    for msg_pk, subject, body_text, from_hdr in messages:
        parts = conn.execute(
            """
            SELECT extracted_text FROM attachments
            WHERE msg_pk = ? AND extracted_text IS NOT NULL AND extracted_text != ''
            """,
            (msg_pk,),
        ).fetchall()
        attachment_text = "\n".join(str(part[0]) for part in parts)
        conn.execute(
            """
            INSERT INTO msg_fts(rowid, subject, body_text, from_hdr, attachment_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (msg_pk, subject or "", body_text or "", from_hdr or "", attachment_text),
        )


def connect(path: Path | str) -> sqlite3.Connection:
    target = str(path)
    memory = target == ":memory:"
    if not memory:
        dest = Path(target)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(dest.parent, 0o700)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Some SQLite builds map the file by default, and mapped pages count as process RSS.
    conn.execute("PRAGMA mmap_size = 0")
    if memory:
        conn.execute("PRAGMA journal_mode = MEMORY")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    if not memory:
        os.chmod(target, 0o600)
    return conn


def init_account(data: Path, alias: str, email: str) -> Path:
    canonical = canonical_alias(alias)
    data.mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    path = data / f"{canonical}.db"
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT email_hash FROM accounts WHERE alias = ?",
            (canonical,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO accounts(alias, email_hash, created_at)
                VALUES (?, ?, ?)
                """,
                (canonical, digest, int(time.time())),
            )
            conn.commit()
        elif row["email_hash"] != digest:
            raise UsageError("email does not match the existing account")
    finally:
        conn.close()
    return path


def ordered_row_dump(conn: sqlite3.Connection) -> str:
    lines: list[str] = []
    for table in DUMP_TABLES:
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        order = ", ".join(cols)
        query = f"SELECT {order} FROM {table} ORDER BY {order}"
        for row in conn.execute(query):
            rendered = "\t".join("" if value is None else str(value) for value in row)
            lines.append(f"{table}|{rendered}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def ordered_row_dump_hash(conn: sqlite3.Connection) -> str:
    return hashlib.sha256(ordered_row_dump(conn).encode()).hexdigest()
