"""Remap localized Takeout names and list the names the table does not know."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from mailcrawl.mbox import mbox_facts, takeout_block
from mailcrawl.takeout_labels import checked_label_map, resolved_label


@dataclass(frozen=True)
class _RepairScope:
    tables: frozenset[str]
    columns: frozenset[str]

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> _RepairScope:
        tables = frozenset(
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
        columns: frozenset[str] = frozenset()
        if "messages" in tables:
            columns = frozenset(str(row[1]) for row in conn.execute("PRAGMA table_info(messages)"))
        return cls(tables, columns)

    def can_remap(self) -> bool:
        return (
            "labels" in self.tables
            and "message_labels" in self.tables
            and "raw_path" in self.columns
            and "gmail_message_id" in self.columns
        )

    def can_refresh_dates(self) -> bool:
        return (
            "date_trusted" in self.columns
            and "internal_date" in self.columns
            and "gmail_message_id" in self.columns
        )


@dataclass(frozen=True)
class TakeoutReport:
    unmapped: tuple[str, ...] = ()


def repair_takeout(
    conn: sqlite3.Connection,
    label_map: dict[str, str] | None = None,
    *,
    rewrite_unprovable: bool = False,
) -> TakeoutReport:
    """Remap mbox labels on an accepted Takeout block. A missing or shifted file is left alone."""
    scope = _RepairScope.load(conn)
    if "messages" not in scope.tables:
        return TakeoutReport()
    mapping = checked_label_map(label_map)
    digest = _map_digest(mapping)
    current = not rewrite_unprovable and _map_is_current(conn, digest)
    if scope.can_remap():
        _drop_unaccepted_mbox_labels(conn)
        if not current:
            _drop_plain_mbox_labels(conn)
            _remap_takeout_labels(conn, mapping)
            _set_is_from_me_from_sent(conn)
    if not current and scope.can_refresh_dates():
        _refresh_dates(conn, scope, rewrite_unprovable=rewrite_unprovable)
    if not current:
        _store_map_digest(conn, digest)
    return _unmapped(conn, mapping)


def takeout_status(
    conn: sqlite3.Connection,
    label_map: dict[str, str] | None = None,
) -> TakeoutReport:
    """List unmapped Takeout names without rewriting labels."""
    return _unmapped(conn, checked_label_map(label_map))


def _drop_unaccepted_mbox_labels(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM message_labels
        WHERE gmail_label_id LIKE 'mbox:%'
          AND msg_pk IN (
            SELECT msg_pk FROM messages WHERE gmail_message_id LIKE 'rfc822:%'
          )
        """
    )
    if _has_table(conn, "threads"):
        conn.execute(
            """
            DELETE FROM message_labels
            WHERE gmail_label_id LIKE 'mbox:%'
              AND msg_pk IN (
                SELECT messages.msg_pk
                FROM messages
                JOIN threads ON threads.thread_pk = messages.thread_pk
                WHERE messages.raw_path LIKE 'mbox:%'
                  AND threads.gmail_thread_id LIKE 'mbox:%'
              )
            """
        )
    _drop_orphan_mbox_labels(conn)


def _set_is_from_me_from_sent(conn: sqlite3.Connection) -> None:
    if "is_from_me" not in {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}:
        return
    if not _has_table(conn, "message_labels"):
        return
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


def _drop_plain_mbox_labels(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT message_labels.msg_pk, message_labels.gmail_label_id,
               messages.raw_path, messages.gmail_message_id
        FROM message_labels
        JOIN messages ON messages.msg_pk = message_labels.msg_pk
        WHERE message_labels.gmail_label_id LIKE 'mbox:%'
        """
    ).fetchall()
    for row in rows:
        facts = mbox_facts(row["raw_path"], str(row["gmail_message_id"]))
        if facts.kind != "plain":
            continue
        conn.execute(
            "DELETE FROM message_labels WHERE msg_pk = ? AND gmail_label_id = ?",
            (int(row["msg_pk"]), str(row["gmail_label_id"])),
        )
    _drop_orphan_mbox_labels(conn)


def _remap_takeout_labels(conn: sqlite3.Connection, mapping: dict[str, str]) -> None:
    rows = conn.execute(
        """
        SELECT message_labels.msg_pk, message_labels.account_id, message_labels.gmail_label_id,
               labels.name, messages.raw_path, messages.gmail_message_id
        FROM message_labels
        JOIN labels
          ON labels.account_id = message_labels.account_id
         AND labels.gmail_label_id = message_labels.gmail_label_id
        JOIN messages ON messages.msg_pk = message_labels.msg_pk
        WHERE message_labels.gmail_label_id LIKE 'mbox:%'
        """
    ).fetchall()
    planned: list[tuple[int, int, str, str]] = []
    for row in rows:
        gmail_message_id = str(row["gmail_message_id"])
        if not takeout_block(row["raw_path"], gmail_message_id):
            continue
        name = str(row["name"])
        target = resolved_label(name, mapping)
        current = str(row["gmail_label_id"])
        if target is None or target == current:
            continue
        planned.append((int(row["msg_pk"]), int(row["account_id"]), current, target))
    for msg_pk, account_id, current, target in planned:
        if not _ensure_system_label(conn, account_id, target):
            continue
        exists = conn.execute(
            """
            SELECT 1 FROM message_labels
            WHERE msg_pk = ? AND gmail_label_id = ?
            """,
            (msg_pk, target),
        ).fetchone()
        if exists is not None:
            conn.execute(
                "DELETE FROM message_labels WHERE msg_pk = ? AND gmail_label_id = ?",
                (msg_pk, current),
            )
        else:
            conn.execute(
                """
                UPDATE message_labels SET gmail_label_id = ?
                WHERE msg_pk = ? AND gmail_label_id = ?
                """,
                (target, msg_pk, current),
            )
    _drop_orphan_mbox_labels(conn)


def _drop_orphan_mbox_labels(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM labels
        WHERE gmail_label_id LIKE 'mbox:%'
          AND NOT EXISTS (
            SELECT 1 FROM message_labels
            WHERE message_labels.account_id = labels.account_id
              AND message_labels.gmail_label_id = labels.gmail_label_id
          )
        """
    )


def _ensure_system_label(conn: sqlite3.Connection, account_id: int, label_id: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM labels WHERE account_id = ? AND gmail_label_id = ?",
        (account_id, label_id),
    ).fetchone()
    if found is not None:
        return True
    named = conn.execute(
        "SELECT 1 FROM labels WHERE account_id = ? AND name = ?",
        (account_id, label_id),
    ).fetchone()
    if named is not None:
        return False
    conn.execute(
        """
        INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
        VALUES (?, ?, ?, 'system', 0)
        """,
        (account_id, label_id, label_id),
    )
    return True


def _map_digest(mapping: dict[str, str]) -> str:
    body = "\n".join(f"{key}={mapping[key]}" for key in sorted(mapping))
    return hashlib.sha256(body.encode()).hexdigest()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _map_is_current(conn: sqlite3.Connection, digest: str) -> bool:
    if not _has_table(conn, "accounts") or not _has_table(conn, "cursors"):
        return False
    accounts = [int(row[0]) for row in conn.execute("SELECT account_id FROM accounts")]
    if not accounts:
        return False
    for account_id in accounts:
        row = conn.execute(
            "SELECT value FROM cursors WHERE account_id = ? AND stream = 'takeout_label_map'",
            (account_id,),
        ).fetchone()
        if row is None or str(row[0]) != digest:
            return False
    return True


def _store_map_digest(conn: sqlite3.Connection, digest: str) -> None:
    if not _has_table(conn, "accounts") or not _has_table(conn, "cursors"):
        return
    for row in conn.execute("SELECT account_id FROM accounts"):
        conn.execute(
            """
            INSERT INTO cursors(account_id, stream, value, updated_at)
            VALUES (?, 'takeout_label_map', ?, 0)
            ON CONFLICT(account_id, stream) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (int(row[0]), digest),
        )


def _unmapped(conn: sqlite3.Connection, mapping: dict[str, str]) -> TakeoutReport:
    if not _has_table(conn, "accounts") or not _has_table(conn, "labels"):
        return TakeoutReport()
    if not _has_table(conn, "messages"):
        return TakeoutReport()
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}
    if "gmail_message_id" not in columns or "raw_path" not in columns:
        return TakeoutReport()
    unmapped: set[str] = set()
    rows = conn.execute(
        """
        SELECT labels.name, messages.raw_path, messages.gmail_message_id
        FROM labels
        JOIN message_labels
          ON message_labels.account_id = labels.account_id
         AND message_labels.gmail_label_id = labels.gmail_label_id
        JOIN messages ON messages.msg_pk = message_labels.msg_pk
        WHERE labels.gmail_label_id LIKE 'mbox:%'
        """
    ).fetchall()
    for row in rows:
        if not takeout_block(row["raw_path"], str(row["gmail_message_id"])):
            continue
        name = str(row["name"])
        if resolved_label(name, mapping) is not None:
            continue
        unmapped.add(name)
    return TakeoutReport(unmapped=tuple(sorted(unmapped)))


def _refresh_dates(conn: sqlite3.Connection, scope: _RepairScope, *, rewrite_unprovable: bool) -> None:
    has_raw = "raw_path" in scope.columns
    query = (
        "SELECT msg_pk, gmail_message_id, internal_date, raw_path FROM messages"
        if has_raw
        else "SELECT msg_pk, gmail_message_id, internal_date FROM messages"
    )
    for row in conn.execute(query):
        msg_pk = int(row[0])
        gmail_message_id = str(row[1])
        raw_path = row[3] if has_raw else None
        mbox = isinstance(raw_path, str) and raw_path.startswith("mbox:")
        if not mbox:
            if rewrite_unprovable:
                conn.execute("UPDATE messages SET date_trusted = 0 WHERE msg_pk = ?", (msg_pk,))
            continue
        facts = mbox_facts(raw_path, gmail_message_id)
        if facts.kind == "unread":
            continue
        if facts.receipt is None:
            if rewrite_unprovable:
                conn.execute("UPDATE messages SET date_trusted = 0 WHERE msg_pk = ?", (msg_pk,))
            continue
        stamp, trust = facts.receipt
        conn.execute(
            "UPDATE messages SET internal_date = ?, date_trusted = ? WHERE msg_pk = ?",
            (stamp, trust, msg_pk),
        )
