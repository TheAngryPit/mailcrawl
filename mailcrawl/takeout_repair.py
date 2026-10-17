"""Apply a configured Takeout label map and report a Sent guess. The guess is not applied."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from mailcrawl.mbox import stored_receipt, takeout_block
from mailcrawl.takeout_labels import checked_label_map, label_key, resolved_label, sent_candidates
from mailcrawl.textnorm import parse_address


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
        )

    def can_settle(self) -> bool:
        return "is_from_me" in self.columns and "message_labels" in self.tables

    def can_refresh_dates(self) -> bool:
        return "date_trusted" in self.columns and "internal_date" in self.columns


def repair_takeout(
    conn: sqlite3.Connection,
    label_map: dict[str, str] | None = None,
    *,
    rewrite_unprovable: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """Remap configured Takeout labels. A Sent guess is reported and not applied."""
    scope = _RepairScope.load(conn)
    if "messages" not in scope.tables:
        return "", ()
    mapping = checked_label_map(label_map)
    digest = _map_digest(mapping)
    if not rewrite_unprovable and _map_is_current(conn, digest):
        return _takeout_report(conn, mapping)
    notes: dict[int, str] | None = None
    if scope.can_remap():
        _remap_takeout_labels(conn, mapping)
        notes = _sent_notes(conn, mapping)
        _write_notes(conn, notes)
    if scope.can_settle():
        _settle_owner(conn)
    if scope.can_refresh_dates():
        _refresh_dates(conn, scope, rewrite_unprovable=rewrite_unprovable)
    _store_map_digest(conn, digest)
    return _takeout_report(conn, mapping, notes)


def takeout_status(
    conn: sqlite3.Connection,
    label_map: dict[str, str] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Report a Sent proposal and unmapped Takeout names without reading mbox files."""
    return _takeout_report(conn, checked_label_map(label_map))


def _remap_takeout_labels(conn: sqlite3.Connection, mapping: dict[str, str]) -> None:
    rows = conn.execute(
        """
        SELECT message_labels.msg_pk, message_labels.account_id, message_labels.gmail_label_id,
               labels.name, messages.raw_path
        FROM message_labels
        JOIN labels
          ON labels.account_id = message_labels.account_id
         AND labels.gmail_label_id = message_labels.gmail_label_id
        JOIN messages ON messages.msg_pk = message_labels.msg_pk
        """
    ).fetchall()
    planned: list[tuple[int, int, str, str]] = []
    for row in rows:
        raw_path = row["raw_path"]
        if not takeout_block(raw_path):
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
        _cursor(conn, int(row[0]), "takeout_label_map", digest)


def _write_notes(conn: sqlite3.Connection, notes: dict[int, str]) -> None:
    if not _has_table(conn, "accounts") or not _has_table(conn, "cursors"):
        return
    for row in conn.execute("SELECT account_id FROM accounts"):
        account_id = int(row[0])
        note = notes.get(account_id, "")
        if note:
            _cursor(conn, account_id, "takeout_sent", note)
        else:
            conn.execute(
                "DELETE FROM cursors WHERE account_id = ? AND stream = 'takeout_sent'",
                (account_id,),
            )


def _sent_notes(conn: sqlite3.Connection, mapping: dict[str, str]) -> dict[int, str]:
    if not _has_table(conn, "accounts"):
        return {}
    forced = sorted(name for name, label_id in mapping.items() if label_id == "SENT")
    if forced:
        note = "override:" + "|".join(forced)
        return {int(row[0]): note for row in conn.execute("SELECT account_id FROM accounts")}
    hashes = {
        int(row[0]): str(row[1])
        for row in conn.execute("SELECT account_id, email_hash FROM accounts")
    }
    sent_known = {
        int(row[0])
        for row in conn.execute(
            "SELECT DISTINCT account_id FROM message_labels WHERE gmail_label_id = 'SENT'"
        )
    }
    counts: dict[int, dict[str, list[int]]] = {}
    rows = conn.execute(
        """
        SELECT message_labels.account_id, labels.name, messages.from_hdr
        FROM message_labels
        JOIN labels
          ON labels.account_id = message_labels.account_id
         AND labels.gmail_label_id = message_labels.gmail_label_id
        JOIN messages ON messages.msg_pk = message_labels.msg_pk
        WHERE message_labels.gmail_label_id LIKE 'mbox:%'
        """
    ).fetchall()
    for row in rows:
        name = str(row["name"])
        if resolved_label(name, mapping) is not None:
            continue
        account_id = int(row["account_id"])
        bucket = counts.setdefault(account_id, {})
        pair = bucket.setdefault(name, [0, 0])
        pair[0] += 1
        digest = _from_hash(row["from_hdr"])
        if digest is not None and digest == hashes.get(account_id):
            pair[1] += 1
    notes: dict[int, str] = {}
    for account_id, buckets in counts.items():
        found = sent_candidates(
            {name: (pair[0], pair[1]) for name, pair in buckets.items()},
            sent_known=account_id in sent_known,
        )
        if len(found) == 1:
            notes[account_id] = "proposed:" + found[0]
        elif len(found) > 1:
            notes[account_id] = "ambiguous:" + "|".join(found)
    return notes


def _takeout_report(
    conn: sqlite3.Connection,
    mapping: dict[str, str],
    notes: dict[int, str] | None = None,
) -> tuple[str, tuple[str, ...]]:
    if not _has_table(conn, "accounts") or not _has_table(conn, "labels"):
        return "", ()
    if notes is None:
        notes = _sent_notes(conn, mapping)
    ordered: list[str] = []
    for account_id in sorted(notes):
        note = notes[account_id]
        if note not in ordered:
            ordered.append(note)
    sent = "|".join(ordered)
    unmapped: set[str] = set()
    rows = conn.execute(
        """
        SELECT labels.name
        FROM labels
        WHERE labels.gmail_label_id LIKE 'mbox:%'
        """
    ).fetchall()
    for row in rows:
        name = str(row[0])
        if resolved_label(name, mapping) is not None:
            continue
        unmapped.add(name)
    return sent, tuple(sorted(unmapped))


def _from_hash(header: object) -> str | None:
    if not isinstance(header, str):
        return None
    _name, address, _domain = parse_address(header)
    if not address:
        return None
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()


def _settle_owner(conn: sqlite3.Connection) -> None:
    if not _has_table(conn, "accounts"):
        return
    accounts = [int(row[0]) for row in conn.execute("SELECT account_id FROM accounts")]
    for account_id in accounts:
        sent = conn.execute(
            """
            SELECT count(*) FROM message_labels
            WHERE account_id = ? AND gmail_label_id = 'SENT'
            """,
            (account_id,),
        ).fetchone()
        if sent is None or int(sent[0]) == 0:
            _cursor(conn, account_id, "owner_flag", "kept")
            continue
        conn.execute(
            "DELETE FROM cursors WHERE account_id = ? AND stream = 'owner_flag'",
            (account_id,),
        )
        conn.execute(
            """
            UPDATE messages
            SET is_from_me = CASE WHEN EXISTS (
              SELECT 1 FROM message_labels
              WHERE message_labels.msg_pk = messages.msg_pk
                AND message_labels.gmail_label_id = 'SENT'
            ) THEN 1 ELSE 0 END
            WHERE account_id = ?
            """,
            (account_id,),
        )


def _cursor(conn: sqlite3.Connection, account_id: int, stream: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO cursors(account_id, stream, value, updated_at)
        VALUES (?, ?, ?, 0)
        ON CONFLICT(account_id, stream) DO UPDATE SET
          value = excluded.value,
          updated_at = excluded.updated_at
        """,
        (account_id, stream, value),
    )


def _refresh_dates(conn: sqlite3.Connection, scope: _RepairScope, *, rewrite_unprovable: bool) -> None:
    has_raw = "raw_path" in scope.columns
    query = (
        "SELECT msg_pk, internal_date, raw_path FROM messages"
        if has_raw
        else "SELECT msg_pk, internal_date FROM messages"
    )
    for row in conn.execute(query):
        msg_pk = int(row[0])
        raw_path = row[2] if has_raw else None
        receipt = stored_receipt(raw_path) if has_raw else None
        if receipt is None:
            if rewrite_unprovable:
                conn.execute("UPDATE messages SET date_trusted = 0 WHERE msg_pk = ?", (msg_pk,))
            continue
        stamp, trust = receipt
        conn.execute(
            "UPDATE messages SET internal_date = ?, date_trusted = ? WHERE msg_pk = ?",
            (stamp, trust, msg_pk),
        )
