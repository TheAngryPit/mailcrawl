"""Open one account database and migrate it to schema v1."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

from mailcrawl.paths import UsageError, canonical_alias
from mailcrawl.schema import SCHEMA_SQL, USER_VERSION

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
    """The database user_version is not 0 or 1."""


def migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version == USER_VERSION:
        return
    if version != 0:
        raise UnsupportedSchema(f"user_version {version}")
    conn.executescript(SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version = {USER_VERSION}")
    conn.commit()


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
