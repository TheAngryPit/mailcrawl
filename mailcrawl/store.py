"""Open one account database and migrate it to the current schema."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from mailcrawl.clock import as_of
from mailcrawl.paths import UsageError, canonical_alias
from mailcrawl.schema import FTS_SQL, JEV_SQL, SCHEMA_SQL, TRIGRAM_SQL, USER_VERSION, V2_TABLES_SQL
from mailcrawl.takeout_repair import repair_takeout
from mailcrawl.textnorm import clean_body
from mailcrawl.threads import mark_image_only

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
    pass


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
        version = 4
    if version == 2:
        _migrate_v2(conn)
        version = 3
    if version == 3:
        _migrate_v3(conn)
        version = 4
    if version == 4:
        _migrate_v4(conn)
        version = 5
    if version == 5:
        _migrate_v5(conn)
        version = 6
    if version == 6:
        _migrate_v6(conn)
        version = 7
    if version == 7:
        _migrate_v7(conn)
        version = 8
    if version == 8:
        _migrate_v8(conn)
        version = 9
    if version == 9:
        _migrate_v9(conn)
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
    conn.execute("PRAGMA user_version = 4")
    conn.commit()


def _migrate_v2(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    conn.execute(
        """
        CREATE TABLE decisions_v3 (
          decision_id INTEGER PRIMARY KEY,
          thread_pk INTEGER NOT NULL REFERENCES threads,
          next_state TEXT NOT NULL CHECK (next_state IN (
            'reply','action','waiting','read_later','reference','noise','needs_review','resolved','screener'
          )),
          priority INTEGER CHECK (priority BETWEEN 0 AND 3),
          owner TEXT CHECK (owner IN ('me','other','none')),
          review_date INTEGER,
          due_date INTEGER,
          lane TEXT CHECK (lane IN ('imbox','feed','paper_trail','screened_out')),
          pinned_aside INTEGER NOT NULL DEFAULT 0,
          muted INTEGER NOT NULL DEFAULT 0,
          confidence REAL,
          source TEXT NOT NULL CHECK (source IN ('rule','embedding-cluster','llm','human','legacy-label')),
          rule_id TEXT,
          rules_version TEXT NOT NULL,
          evidence TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          superseded_by INTEGER REFERENCES decisions_v3(decision_id),
          CHECK (next_state <> 'waiting' OR review_date IS NOT NULL)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO decisions_v3(
          decision_id, thread_pk, next_state, priority, owner, review_date, due_date,
          lane, pinned_aside, muted, confidence, source, rule_id, rules_version,
          evidence, created_at, superseded_by
        )
        SELECT
          decision_id, thread_pk, next_state, priority, owner, review_date, due_date,
          lane, pinned_aside, muted, confidence, source, rule_id, rules_version,
          evidence, created_at, superseded_by
        FROM decisions
        """
    )
    conn.execute("DROP TABLE decisions")
    conn.execute("ALTER TABLE decisions_v3 RENAME TO decisions")
    conn.execute(
        """
        CREATE UNIQUE INDEX decisions_current
        ON decisions(thread_pk) WHERE superseded_by IS NULL
        """
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    if "origin" in columns and "source_detail" in columns:
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        return
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    conn.execute(
        """
        CREATE TABLE decisions_v4 (
          decision_id INTEGER PRIMARY KEY,
          thread_pk INTEGER NOT NULL REFERENCES threads,
          next_state TEXT NOT NULL CHECK (next_state IN (
            'reply','action','waiting','read_later','reference','noise','needs_review','resolved','screener'
          )),
          priority INTEGER CHECK (priority BETWEEN 0 AND 3),
          owner TEXT CHECK (owner IN ('me','other','none')),
          review_date INTEGER,
          due_date INTEGER,
          lane TEXT CHECK (lane IN ('imbox','feed','paper_trail','screened_out')),
          pinned_aside INTEGER NOT NULL DEFAULT 0,
          muted INTEGER NOT NULL DEFAULT 0,
          confidence REAL,
          source TEXT NOT NULL CHECK (source IN ('rule','embedding-cluster','llm','human','legacy-label')),
          origin TEXT NOT NULL CHECK (origin IN ('rule','knn','model','human')),
          source_detail TEXT NOT NULL,
          rule_id TEXT,
          rules_version TEXT NOT NULL,
          evidence TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          superseded_by INTEGER REFERENCES decisions_v4(decision_id),
          CHECK (next_state <> 'waiting' OR review_date IS NOT NULL)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO decisions_v4(
          decision_id, thread_pk, next_state, priority, owner, review_date, due_date,
          lane, pinned_aside, muted, confidence, source, origin, source_detail,
          rule_id, rules_version, evidence, created_at, superseded_by
        )
        SELECT
          decision_id, thread_pk, next_state, priority, owner, review_date, due_date,
          lane, pinned_aside, muted, confidence, source,
          CASE source
            WHEN 'embedding-cluster' THEN 'knn'
            WHEN 'llm' THEN 'model'
            WHEN 'human' THEN 'human'
            WHEN 'legacy-label' THEN 'human'
            ELSE 'rule'
          END,
          COALESCE(rule_id, ''),
          rule_id, rules_version, evidence, created_at, superseded_by
        FROM decisions
        """
    )
    conn.execute("DROP TABLE decisions")
    conn.execute("ALTER TABLE decisions_v4 RENAME TO decisions")
    conn.execute(
        """
        CREATE UNIQUE INDEX decisions_current
        ON decisions(thread_pk) WHERE superseded_by IS NULL
        """
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _execute_statements(conn: sqlite3.Connection, script: str) -> None:
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ""
    if pending.strip():
        conn.execute(pending)


def _migrate_v4(conn: sqlite3.Connection) -> None:
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "threads" in names:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        if "image_only" not in columns:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN image_only INTEGER NOT NULL DEFAULT 0"
            )
        mark_image_only(conn)
    if "messages" in names and "attachments" in names:
        _rebuild_fts(conn)
    else:
        if "msg_fts" not in names:
            _execute_statements(conn, FTS_SQL)
        if "msg_trigram" not in names:
            _execute_statements(conn, TRIGRAM_SQL)
    conn.execute("PRAGMA user_version = 5")
    conn.commit()


def _migrate_v5(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA user_version = 6")
    conn.commit()


def _migrate_v6(conn: sqlite3.Connection) -> None:
    _execute_statements(conn, JEV_SQL)
    conn.execute("PRAGMA user_version = 7")
    conn.commit()


def _migrate_v7(conn: sqlite3.Connection) -> None:
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "messages" in names:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}
        if "date_trusted" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN date_trusted INTEGER NOT NULL DEFAULT 1")
        if "is_from_me" in columns and "message_labels" in names:
            conn.execute(
                """
                UPDATE messages
                SET is_from_me = CASE WHEN EXISTS (
                  SELECT 1 FROM message_labels
                  WHERE message_labels.msg_pk = messages.msg_pk
                    AND message_labels.gmail_label_id = 'SENT'
                ) THEN 1 ELSE 0 END
                """
            )
    conn.execute("PRAGMA user_version = 8")
    conn.commit()


def _migrate_v8(conn: sqlite3.Connection) -> None:
    repair_takeout(conn, rewrite_unprovable=True)
    conn.execute("PRAGMA user_version = 9")
    conn.commit()


def _migrate_v9(conn: sqlite3.Connection) -> None:
    names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "threads" in names:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(threads)")}
        if "last_msg_from_me" in columns:
            conn.execute("ALTER TABLE threads DROP COLUMN last_msg_from_me")
    if "takeout_label_override" in names:
        conn.execute("DROP TABLE takeout_label_override")
    if "cursors" in names:
        conn.execute(
            "DELETE FROM cursors WHERE stream IN ('takeout_sent', 'owner_flag')"
        )
    conn.execute(f"PRAGMA user_version = {USER_VERSION}")
    conn.commit()


def index_message(
    conn: sqlite3.Connection,
    msg_pk: int,
    subject: str | None,
    body_text: str | None,
    from_hdr: str | None,
    attachment_text: str | None,
) -> None:
    values = (msg_pk, subject or "", body_text or "", from_hdr or "", attachment_text or "")
    for table in ("msg_fts", "msg_trigram"):
        conn.execute(
            f"""
            INSERT INTO {table}(rowid, subject, body_text, from_hdr, attachment_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            values,
        )


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS msg_fts")
    conn.execute("DROP TABLE IF EXISTS msg_trigram")
    _execute_statements(conn, FTS_SQL)
    _execute_statements(conn, TRIGRAM_SQL)
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
        index_message(conn, msg_pk, subject, body_text, from_hdr, attachment_text)


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
                (canonical, digest, as_of(None)),
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
