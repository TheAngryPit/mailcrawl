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


@dataclass(frozen=True)
class TakeoutReport:
    sent: str = ""
    unmapped: tuple[str, ...] = ()
    skipped: tuple[tuple[str, int], ...] = ()
    warnings: tuple[str, ...] = ()


def repair_takeout(
    conn: sqlite3.Connection,
    label_map: dict[str, str] | None = None,
    *,
    rewrite_unprovable: bool = False,
    owners: frozenset[str] | None = None,
) -> TakeoutReport:
    """Remap configured Takeout labels. A Sent guess is reported and not applied."""
    scope = _RepairScope.load(conn)
    if "messages" not in scope.tables:
        return TakeoutReport()
    mapping = checked_label_map(label_map)
    digest = _map_digest(mapping)
    current = not rewrite_unprovable and _map_is_current(conn, digest)
    notes: dict[int, str] | None = None
    if scope.can_remap():
        _ensure_override_table(conn)
        _revert_sent_grants(conn, mapping)
    if scope.can_remap() and not current:
        _remap_takeout_labels(conn, mapping)
    if scope.can_remap():
        _grant_sent(conn, mapping, owners or frozenset())
        notes = _sent_notes(conn, mapping)
        _write_notes(conn, notes)
    if scope.can_settle():
        _settle_owner(conn)
    if not current and scope.can_refresh_dates():
        _refresh_dates(conn, scope, rewrite_unprovable=rewrite_unprovable)
    if not current:
        _store_map_digest(conn, digest)
    return _takeout_report(conn, mapping, notes, owners or frozenset())


def takeout_status(
    conn: sqlite3.Connection,
    label_map: dict[str, str] | None = None,
    *,
    owners: frozenset[str] | None = None,
) -> TakeoutReport:
    """Report a Sent proposal and unmapped Takeout names without reading mbox files."""
    return _takeout_report(conn, checked_label_map(label_map), None, owners or frozenset())


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
        WHERE message_labels.gmail_label_id LIKE 'mbox:%'
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


def _ensure_override_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS takeout_label_override (
          msg_pk INTEGER NOT NULL,
          label_key TEXT NOT NULL,
          previous_id TEXT NOT NULL,
          applied_id TEXT NOT NULL,
          replaced INTEGER NOT NULL,
          PRIMARY KEY (msg_pk, label_key)
        )
        """
    )


def _sent_keys(mapping: dict[str, str]) -> frozenset[str]:
    return frozenset(key for key, label_id in mapping.items() if label_id == "SENT")


def _revert_sent_grants(conn: sqlite3.Connection, mapping: dict[str, str]) -> None:
    if not _has_table(conn, "takeout_label_override"):
        return
    kept = _sent_keys(mapping)
    rows = conn.execute(
        """
        SELECT takeout_label_override.msg_pk, takeout_label_override.label_key,
               takeout_label_override.previous_id, takeout_label_override.applied_id,
               takeout_label_override.replaced, messages.account_id
        FROM takeout_label_override
        JOIN messages ON messages.msg_pk = takeout_label_override.msg_pk
        """
    ).fetchall()
    staying: dict[int, list[sqlite3.Row]] = {}
    reverting: list[sqlite3.Row] = []
    for row in rows:
        if str(row["label_key"]) in kept:
            staying.setdefault(int(row["msg_pk"]), []).append(row)
        else:
            reverting.append(row)
    for row in reverting:
        msg_pk = int(row["msg_pk"])
        account_id = int(row["account_id"])
        previous_id = str(row["previous_id"])
        applied_id = str(row["applied_id"])
        replaced = int(row["replaced"])
        if not _ensure_mbox_label(conn, account_id, previous_id):
            continue
        others = staying.get(msg_pk, [])
        if replaced == 0 or others:
            if not _message_has_label(conn, msg_pk, previous_id):
                _insert_message_label(conn, msg_pk, account_id, previous_id)
            if replaced == 1:
                _promote_sent_grant(conn, others)
        elif _message_has_label(conn, msg_pk, applied_id) and not _message_has_label(
            conn, msg_pk, previous_id
        ):
            conn.execute(
                """
                UPDATE message_labels SET gmail_label_id = ?
                WHERE msg_pk = ? AND gmail_label_id = ?
                """,
                (previous_id, msg_pk, applied_id),
            )
        elif not _message_has_label(conn, msg_pk, previous_id):
            _insert_message_label(conn, msg_pk, account_id, previous_id)
        elif _message_has_label(conn, msg_pk, applied_id):
            conn.execute(
                "DELETE FROM message_labels WHERE msg_pk = ? AND gmail_label_id = ?",
                (msg_pk, applied_id),
            )
        if not _message_has_label(conn, msg_pk, "SENT"):
            conn.execute("UPDATE messages SET is_from_me = 0 WHERE msg_pk = ?", (msg_pk,))
        conn.execute(
            "DELETE FROM takeout_label_override WHERE msg_pk = ? AND label_key = ?",
            (msg_pk, str(row["label_key"])),
        )
    _drop_orphan_mbox_labels(conn)


def _promote_sent_grant(conn: sqlite3.Connection, others: list[sqlite3.Row]) -> None:
    for other in others:
        if int(other["replaced"]) != 0:
            continue
        conn.execute(
            """
            UPDATE takeout_label_override SET replaced = 1
            WHERE msg_pk = ? AND label_key = ?
            """,
            (int(other["msg_pk"]), str(other["label_key"])),
        )
        return


def _grant_sent(
    conn: sqlite3.Connection,
    mapping: dict[str, str],
    owners: frozenset[str],
) -> None:
    keys = _sent_keys(mapping)
    if not keys or not _has_table(conn, "accounts"):
        return
    hashes = {
        int(row[0]): _owner_hashes(str(row[1]), owners)
        for row in conn.execute("SELECT account_id, email_hash FROM accounts")
    }
    rows = conn.execute(
        """
        SELECT message_labels.msg_pk, message_labels.account_id,
               message_labels.gmail_label_id, labels.name, messages.from_hdr
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
        key = label_key(name)
        if key not in keys:
            continue
        msg_pk = int(row["msg_pk"])
        recorded = conn.execute(
            """
            SELECT 1 FROM takeout_label_override
            WHERE msg_pk = ? AND label_key = ?
            """,
            (msg_pk, key),
        ).fetchone()
        if recorded is not None:
            continue
        account_id = int(row["account_id"])
        digest = _from_hash(row["from_hdr"])
        if digest is None or digest not in hashes.get(account_id, frozenset()):
            continue
        if not _ensure_system_label(conn, account_id, "SENT"):
            continue
        current = str(row["gmail_label_id"])
        if _message_has_label(conn, msg_pk, "SENT"):
            conn.execute(
                "DELETE FROM message_labels WHERE msg_pk = ? AND gmail_label_id = ?",
                (msg_pk, current),
            )
            replaced = 0
        else:
            conn.execute(
                """
                UPDATE message_labels SET gmail_label_id = 'SENT'
                WHERE msg_pk = ? AND gmail_label_id = ?
                """,
                (msg_pk, current),
            )
            replaced = 1
        conn.execute(
            """
            INSERT INTO takeout_label_override(
              msg_pk, label_key, previous_id, applied_id, replaced
            ) VALUES (?, ?, ?, 'SENT', ?)
            """,
            (msg_pk, key, current, replaced),
        )
    _drop_orphan_mbox_labels(conn)


def _owner_hashes(email_hash: str, owners: frozenset[str]) -> frozenset[str]:
    found = {email_hash}
    for owner in owners:
        address = owner.strip().lower()
        if address:
            found.add(hashlib.sha256(address.encode()).hexdigest())
    return frozenset(found)


def _ensure_mbox_label(conn: sqlite3.Connection, account_id: int, label_id: str) -> bool:
    if not label_id.startswith("mbox:"):
        return False
    found = conn.execute(
        "SELECT 1 FROM labels WHERE account_id = ? AND gmail_label_id = ?",
        (account_id, label_id),
    ).fetchone()
    if found is not None:
        return True
    name = label_id.removeprefix("mbox:")
    named = conn.execute(
        "SELECT gmail_label_id FROM labels WHERE account_id = ? AND name = ?",
        (account_id, name),
    ).fetchone()
    if named is not None:
        return str(named[0]) == label_id
    conn.execute(
        """
        INSERT INTO labels(account_id, gmail_label_id, name, kind, seen_at)
        VALUES (?, ?, ?, 'user', 0)
        """,
        (account_id, label_id, name),
    )
    return True


def _message_has_label(conn: sqlite3.Connection, msg_pk: int, label_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM message_labels WHERE msg_pk = ? AND gmail_label_id = ?",
        (msg_pk, label_id),
    ).fetchone()
    return row is not None


def _insert_message_label(
    conn: sqlite3.Connection, msg_pk: int, account_id: int, label_id: str
) -> None:
    conn.execute(
        """
        INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
        VALUES (?, ?, ?, 0)
        """,
        (msg_pk, account_id, label_id),
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
    notes: dict[int, str] | None,
    owners: frozenset[str],
) -> TakeoutReport:
    if not _has_table(conn, "accounts") or not _has_table(conn, "labels"):
        return TakeoutReport()
    if notes is None:
        notes = _sent_notes(conn, mapping)
    ordered: list[str] = []
    for account_id in sorted(notes):
        note = notes[account_id]
        if note not in ordered:
            ordered.append(note)
    unmapped: set[str] = set()
    rows = conn.execute(
        """
        SELECT labels.name
        FROM labels
        WHERE labels.gmail_label_id LIKE 'mbox:%'
        """
    ).fetchall()
    sent_names = _sent_keys(mapping)
    for row in rows:
        name = str(row[0])
        if label_key(name) in sent_names:
            continue
        if resolved_label(name, mapping) is not None:
            continue
        unmapped.add(name)
    return TakeoutReport(
        sent="|".join(ordered),
        unmapped=tuple(sorted(unmapped)),
        skipped=_skipped(conn, mapping, owners),
        warnings=_proposal_warnings(conn, notes),
    )


def _skipped(
    conn: sqlite3.Connection,
    mapping: dict[str, str],
    owners: frozenset[str],
) -> tuple[tuple[str, int], ...]:
    keys = _sent_keys(mapping)
    if not keys or not _has_table(conn, "message_labels") or not _has_table(conn, "accounts"):
        return ()
    hashes = {
        int(row[0]): _owner_hashes(str(row[1]), owners)
        for row in conn.execute("SELECT account_id, email_hash FROM accounts")
    }
    counts: dict[str, int] = {}
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
        if label_key(name) not in keys:
            continue
        digest = _from_hash(row["from_hdr"])
        allowed = hashes.get(int(row["account_id"]), frozenset())
        if digest is not None and digest in allowed:
            continue
        counts[name] = counts.get(name, 0) + 1
    return tuple(sorted(counts.items()))


def _proposal_warnings(conn: sqlite3.Connection, notes: dict[int, str]) -> tuple[str, ...]:
    names: set[str] = set()
    for note in notes.values():
        if note.startswith("proposed:"):
            names.add(note.removeprefix("proposed:"))
        elif note.startswith("ambiguous:"):
            names.update(part for part in note.removeprefix("ambiguous:").split("|") if part)
    if not names or not _has_table(conn, "message_labels"):
        return ()
    warned = [name for name in sorted(names) if not _inbox_overlap(conn, name)]
    return tuple(warned)


def _inbox_overlap(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM message_labels
        JOIN labels
          ON labels.account_id = message_labels.account_id
         AND labels.gmail_label_id = message_labels.gmail_label_id
        JOIN message_labels AS inbox
          ON inbox.msg_pk = message_labels.msg_pk
         AND inbox.gmail_label_id = 'INBOX'
        WHERE labels.name = ? AND message_labels.gmail_label_id LIKE 'mbox:%'
        LIMIT 1
        """,
        (name,),
    ).fetchone()
    return row is not None


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
