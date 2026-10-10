"""Open one account database and migrate it to schema v1."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from mailcrawl.schema import SCHEMA_SQL, USER_VERSION


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
