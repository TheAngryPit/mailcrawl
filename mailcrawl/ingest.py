"""Idempotent import of a gog export or a Takeout mbox through one writer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from mailcrawl.contract import (
    AttachmentRec,
    ExportDocument,
    LabelRec,
    MessageRec,
    infer_kind,
    is_pseudo_label,
    parse_export,
)
from mailcrawl.gognative import load_labels, load_native
from mailcrawl.mbox import PROVISIONAL, RAW_PATH, MboxSource, Refused
from mailcrawl.paths import UsageError
from mailcrawl.textnorm import clean_body, parse_address, subject_norm
from mailcrawl.version import mailcrawl_version, provenance_hash

Item = LabelRec | MessageRec | Refused


class ImportResult:
    def __init__(
        self,
        *,
        n_seen: int,
        n_new: int,
        n_dup: int,
        n_error: int,
        n_refused: int = 0,
        n_reconciled: int = 0,
    ) -> None:
        self.n_seen = n_seen
        self.n_new = n_new
        self.n_dup = n_dup
        self.n_error = n_error
        self.n_refused = n_refused
        self.n_reconciled = n_reconciled


def load_catalogue_entries(path: Path | None) -> dict[str, dict[str, str | None]]:
    if path is None:
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    entries: dict[str, dict[str, str | None]] = {}
    for label in document.get("labels") or []:
        if not isinstance(label, dict):
            continue
        name = label.get("name")
        if not isinstance(name, str) or not name:
            continue
        key = label.get("key")
        axis = label.get("axis")
        entries[name] = {
            "key": key if isinstance(key, str) and key else None,
            "axis": axis if isinstance(axis, str) and axis else None,
        }
    return entries


def load_catalogue_names(path: Path | None) -> set[str]:
    return set(load_catalogue_entries(path))


def import_gog_export(
    conn: sqlite3.Connection,
    *,
    alias: str,
    export_dir: Path,
    taxonomy_path: Path | None,
    account_email: str,
) -> ImportResult:
    parsed, manifest_sha = _load_export(export_dir)
    account_id = _account_id(conn, alias)
    return _ingest(
        conn,
        account_id=account_id,
        alias=alias,
        source="gog-export",
        items=[*_label_map(parsed).values(), *parsed.messages],
        ref=lambda: manifest_sha,
        run_json=parsed.raw_json,
        catalogue=load_catalogue_entries(taxonomy_path),
        account_email=account_email,
    )


def import_mbox(
    conn: sqlite3.Connection,
    *,
    alias: str,
    mbox_path: Path,
    labels_path: Path | None,
    taxonomy_path: Path | None,
    account_email: str,
) -> ImportResult:
    account_id = _account_id(conn, alias)
    seed = load_labels(labels_path) if labels_path is not None else ()
    known = {
        row["name"]: LabelRec(row["gmail_label_id"], row["name"], row["kind"])
        for row in conn.execute(
            "SELECT gmail_label_id, name, kind FROM labels WHERE account_id = ? AND gmail_label_id NOT LIKE ?",
            (account_id, PROVISIONAL + "%"),
        )
    }
    known.update({label.name: label for label in seed})
    source = MboxSource(
        mbox_path,
        known,
        seed,
        stored=lambda key: conn.execute(
            "SELECT 1 FROM messages WHERE account_id = ? AND gmail_message_id = ?",
            (account_id, key),
        ).fetchone()
        is not None,
    )
    return _ingest(
        conn,
        account_id=account_id,
        alias=alias,
        source="mbox",
        items=source.items(),
        ref=source.sha256,
        run_json=None,
        catalogue=load_catalogue_entries(taxonomy_path),
        account_email=account_email,
    )


def _account_id(conn: sqlite3.Connection, alias: str) -> int:
    account = conn.execute(
        "SELECT account_id FROM accounts WHERE alias = ?",
        (alias,),
    ).fetchone()
    if account is None:
        raise UsageError("account is not initialized")
    return int(account["account_id"])


def _ingest(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    alias: str,
    source: str,
    items: Iterable[Item],
    ref: Callable[[], str],
    run_json: str | None,
    catalogue: dict[str, dict[str, str | None]],
    account_email: str,
) -> ImportResult:
    version = mailcrawl_version()
    me = account_email.strip().lower()
    started = int(time.time())
    counts = {"new": 0, "dup": 0, "conflict": 0, "reconciled": 0}
    conflicts: list[str] = []
    reconciled: list[dict[str, str]] = []
    refused: list[dict[str, object]] = []
    try:
        for item in items:
            if isinstance(item, LabelRec):
                if source == "mbox" or not is_pseudo_label(item.gmail_label_id):
                    _upsert_label(conn, account_id, item, catalogue)
                continue
            if isinstance(item, Refused):
                refused.append({"offset": item.offset, "reason": item.reason})
                continue
            outcome, kept_sha = _upsert_message(
                conn,
                account_id=account_id,
                alias=alias,
                source=source,
                message=item,
                version=version,
                me=me,
            )
            counts[outcome] += 1
            if outcome == "conflict":
                conflicts.append(item.gmail_message_id)
            elif outcome == "reconciled":
                reconciled.append(
                    {
                        "gmail_message_id": item.gmail_message_id,
                        "kept_raw_sha256": kept_sha,
                        "seen_raw_sha256": item.raw_sha256,
                    }
                )
        manifest_sha = ref()
        n_seen = sum(counts.values()) + len(refused)
        _drop_empty_provisional_threads(conn, account_id)
        _recompute_threads(conn, account_id)
        _recompute_senders(conn, account_id)
        _touch_label_seen(conn, account_id)
        updated_at = conn.execute(
            "SELECT COALESCE(max(internal_date), 0) FROM messages WHERE account_id = ?",
            (account_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO cursors(account_id, stream, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id, stream) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (account_id, source, manifest_sha, updated_at),
        )
        finished = int(time.time())
        conn.execute(
            """
            INSERT INTO import_runs(
              account_id, source, source_manifest_sha256, started_at, finished_at,
              n_seen, n_new, n_dup, n_error, mailcrawl_version, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                source,
                manifest_sha,
                started,
                finished,
                n_seen,
                counts["new"],
                counts["dup"] + counts["reconciled"],
                counts["conflict"],
                version,
                run_json,
            ),
        )
        _append_ledger(
            conn,
            account_id=account_id,
            ref=manifest_sha,
            detail={
                "conflicts": conflicts,
                "n_dup": counts["dup"],
                "n_error": counts["conflict"],
                "n_new": counts["new"],
                "n_reconciled": counts["reconciled"],
                "n_refused": len(refused),
                "n_seen": n_seen,
                "reconciled": reconciled,
                "refused": refused,
                "source": source,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return ImportResult(
        n_seen=n_seen,
        n_new=counts["new"],
        n_dup=counts["dup"],
        n_error=counts["conflict"],
        n_refused=len(refused),
        n_reconciled=counts["reconciled"],
    )


def _load_export(export_dir: Path) -> tuple[ExportDocument, str]:
    export_path = export_dir / "export.json"
    if export_path.is_file():
        raw_file = export_path.read_bytes()
        document = json.loads(raw_file.decode("utf-8"))
        return parse_export(document, export_dir), hashlib.sha256(raw_file).hexdigest()
    return load_native(export_dir)


def _label_map(parsed: ExportDocument) -> dict[str, LabelRec]:
    labels = {label.gmail_label_id: label for label in parsed.labels}
    for message in parsed.messages:
        for label_id in message.label_ids:
            if label_id not in labels:
                labels[label_id] = LabelRec(label_id, label_id, infer_kind(label_id))
    return labels


def _upsert_label(
    conn: sqlite3.Connection,
    account_id: int,
    label: LabelRec,
    catalogue: dict[str, dict[str, str | None]],
) -> None:
    pseudo = is_pseudo_label(label.gmail_label_id)
    provisional = PROVISIONAL + label.name
    adopting = not pseudo and label.gmail_label_id != provisional and conn.execute(
        "SELECT 1 FROM labels WHERE account_id = ? AND gmail_label_id = ? AND name = ?",
        (account_id, provisional, label.name),
    ).fetchone() is not None
    if adopting:
        conn.execute(
            "UPDATE labels SET name = gmail_label_id WHERE account_id = ? AND gmail_label_id = ?",
            (account_id, provisional),
        )
    entry = None if pseudo else catalogue.get(label.name)
    in_catalogue = 1 if entry is not None else 0
    catalogue_key = None
    axis = None
    if entry is not None:
        catalogue_key = entry["key"] or label.name
        axis = entry["axis"]
    conn.execute(
        """
        INSERT INTO labels(
          account_id, gmail_label_id, name, kind, axis, in_catalogue, catalogue_key, seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(account_id, gmail_label_id) DO UPDATE SET
          name = excluded.name,
          kind = excluded.kind,
          axis = excluded.axis,
          in_catalogue = excluded.in_catalogue,
          catalogue_key = excluded.catalogue_key
        """,
        (
            account_id,
            label.gmail_label_id,
            label.name,
            label.kind,
            axis,
            in_catalogue,
            catalogue_key,
        ),
    )
    if adopting:
        conn.execute(
            """
            INSERT OR IGNORE INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
            SELECT msg_pk, account_id, ?, observed_at FROM message_labels
            WHERE account_id = ? AND gmail_label_id = ?
            """,
            (label.gmail_label_id, account_id, provisional),
        )
        conn.execute(
            "DELETE FROM message_labels WHERE account_id = ? AND gmail_label_id = ?",
            (account_id, provisional),
        )
        conn.execute(
            "DELETE FROM labels WHERE account_id = ? AND gmail_label_id = ?",
            (account_id, provisional),
        )


def _upsert_message(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    alias: str,
    source: str,
    message: MessageRec,
    version: str,
    me: str,
) -> tuple[str, str | None]:
    """Return the outcome and, when a row already existed, its kept ``raw_sha256``.

    A later gog import keeps an existing mbox row even when the hash differs,
    refreshes its labels, and moves it off a provisional thread. An mbox import
    never touches a row that came from another source.
    """
    thread_pk = _ensure_thread(conn, account_id, message.gmail_thread_id)
    sender_id, address = _ensure_sender(conn, account_id, message.from_hdr)
    is_from_me = 1 if address is not None and address == me else 0
    prov = provenance_hash(alias, message.gmail_message_id, message.raw_sha256, version)
    raw_body = message.body_text
    cleaned = clean_body(raw_body)
    inserted = conn.execute(
        """
        INSERT INTO messages(
          account_id, gmail_message_id, thread_pk, gmail_history_id, internal_date,
          rfc822_message_id, in_reply_to, references_hdr, sender_id,
          from_hdr, to_hdr, cc_hdr, bcc_hdr, reply_to_hdr,
          subject, snippet, body_text_raw, body_text, size_bytes, is_from_me,
          raw_path, raw_sha256, raw_json, auth_results, provenance_hash, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id, gmail_message_id) DO NOTHING
        RETURNING msg_pk
        """,
        (
            account_id,
            message.gmail_message_id,
            thread_pk,
            message.gmail_history_id,
            message.internal_date,
            message.rfc822_message_id,
            message.in_reply_to,
            message.references_hdr,
            sender_id,
            message.from_hdr,
            message.to_hdr,
            message.cc_hdr,
            message.bcc_hdr,
            message.reply_to_hdr,
            message.subject,
            message.snippet,
            raw_body,
            cleaned,
            message.size_bytes,
            is_from_me,
            message.raw_path,
            message.raw_sha256,
            message.raw_json,
            message.auth_results,
            prov,
            int(time.time()),
        ),
    ).fetchone()
    if inserted is not None:
        msg_pk = int(inserted["msg_pk"])
        _write_children(conn, account_id, msg_pk, message, source)
        return "new", None
    existing = conn.execute(
        """
        SELECT messages.msg_pk, messages.raw_sha256, messages.raw_path, threads.gmail_thread_id
        FROM messages JOIN threads ON threads.thread_pk = messages.thread_pk
        WHERE messages.account_id = ? AND messages.gmail_message_id = ?
        """,
        (account_id, message.gmail_message_id),
    ).fetchone()
    kept_sha = str(existing["raw_sha256"])
    from_mbox = str(existing["raw_path"] or "").startswith(RAW_PATH)
    if source == "mbox" and not from_mbox:
        return "dup", kept_sha
    if kept_sha == message.raw_sha256:
        outcome = "dup"
    elif from_mbox and source != "mbox":
        outcome = "reconciled"
    else:
        return "conflict", kept_sha
    msg_pk = int(existing["msg_pk"])
    _replace_labels(conn, account_id, msg_pk, message, source)
    current = str(existing["gmail_thread_id"])
    incoming = message.gmail_thread_id
    if current != incoming and current.startswith(PROVISIONAL) and not incoming.startswith(PROVISIONAL):
        conn.execute("UPDATE messages SET thread_pk = ? WHERE msg_pk = ?", (thread_pk, msg_pk))
    return outcome, kept_sha


def _write_children(
    conn: sqlite3.Connection,
    account_id: int,
    msg_pk: int,
    message: MessageRec,
    source: str,
) -> None:
    for name, value in message.headers:
        conn.execute(
            "INSERT INTO headers(msg_pk, name, value) VALUES (?, ?, ?)",
            (msg_pk, name, value),
        )
    _replace_labels(conn, account_id, msg_pk, message, source)
    for attachment in message.attachments:
        _insert_attachment(conn, msg_pk, attachment)
    attachment_text = "\n".join(
        item.extracted_text for item in message.attachments if item.extracted_text
    )
    conn.execute(
        """
        INSERT INTO msg_fts(rowid, subject, body_text, from_hdr, attachment_text)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            msg_pk,
            message.subject or "",
            clean_body(message.body_text) or "",
            message.from_hdr or "",
            attachment_text,
        ),
    )


def _replace_labels(
    conn: sqlite3.Connection,
    account_id: int,
    msg_pk: int,
    message: MessageRec,
    source: str,
) -> None:
    """Only an mbox import writes or replaces ``pseudo:`` labels. Other sources keep them."""
    from_mbox = source == "mbox"
    conn.execute(
        "DELETE FROM message_labels WHERE msg_pk = ? AND (? OR gmail_label_id NOT LIKE 'pseudo:%')",
        (msg_pk, from_mbox),
    )
    for label_id in message.label_ids:
        if is_pseudo_label(label_id) and not from_mbox:
            continue
        conn.execute(
            """
            INSERT INTO message_labels(msg_pk, account_id, gmail_label_id, observed_at)
            VALUES (?, ?, ?, ?)
            """,
            (msg_pk, account_id, label_id, message.internal_date),
        )


def _insert_attachment(
    conn: sqlite3.Connection,
    msg_pk: int,
    attachment: AttachmentRec,
) -> None:
    conn.execute(
        """
        INSERT INTO attachments(
          msg_pk, filename, mime_type, size_bytes, gmail_attachment_id,
          content_id, is_inline, file_sha256, extract_status, extract_method,
          extracted_text, ocr_status, ocr_conf, page_count, parser_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            msg_pk,
            attachment.filename,
            attachment.mime_type,
            attachment.size_bytes,
            attachment.attachment_id,
            attachment.content_id,
            attachment.is_inline,
            attachment.file_sha256,
            attachment.extract_status,
            attachment.extract_method,
            attachment.extracted_text,
            attachment.ocr_status,
            attachment.ocr_conf,
            attachment.page_count,
            attachment.parser_version,
        ),
    )


def _ensure_thread(conn: sqlite3.Connection, account_id: int, gmail_thread_id: str) -> int:
    row = conn.execute(
        """
        INSERT INTO threads(account_id, gmail_thread_id)
        VALUES (?, ?)
        ON CONFLICT(account_id, gmail_thread_id) DO NOTHING
        RETURNING thread_pk
        """,
        (account_id, gmail_thread_id),
    ).fetchone()
    if row is not None:
        return int(row["thread_pk"])
    found = conn.execute(
        "SELECT thread_pk FROM threads WHERE account_id = ? AND gmail_thread_id = ?",
        (account_id, gmail_thread_id),
    ).fetchone()
    return int(found["thread_pk"])


def _ensure_sender(
    conn: sqlite3.Connection,
    account_id: int,
    from_hdr: str | None,
) -> tuple[int | None, str | None]:
    display, address, domain = parse_address(from_hdr)
    if address is None or domain is None:
        return None, None
    row = conn.execute(
        """
        INSERT INTO senders(account_id, address_norm, domain, display_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(account_id, address_norm) DO NOTHING
        RETURNING sender_id
        """,
        (account_id, address, domain, display),
    ).fetchone()
    if row is not None:
        return int(row["sender_id"]), address
    found = conn.execute(
        "SELECT sender_id FROM senders WHERE account_id = ? AND address_norm = ?",
        (account_id, address),
    ).fetchone()
    if display:
        conn.execute(
            """
            UPDATE senders SET display_name = ?
            WHERE sender_id = ? AND (display_name IS NULL OR display_name = '')
            """,
            (display, found["sender_id"]),
        )
    return int(found["sender_id"]), address


def _drop_empty_provisional_threads(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute(
        """
        DELETE FROM threads
        WHERE account_id = ? AND gmail_thread_id LIKE ?
          AND NOT EXISTS (SELECT 1 FROM messages WHERE messages.thread_pk = threads.thread_pk)
          AND NOT EXISTS (SELECT 1 FROM decisions WHERE decisions.thread_pk = threads.thread_pk)
          AND NOT EXISTS (SELECT 1 FROM thread_mutes WHERE thread_mutes.thread_pk = threads.thread_pk)
        """,
        (account_id, PROVISIONAL + "%"),
    )


def _recompute_threads(conn: sqlite3.Connection, account_id: int) -> None:
    threads = conn.execute(
        "SELECT thread_pk FROM threads WHERE account_id = ?",
        (account_id,),
    ).fetchall()
    for thread in threads:
        thread_pk = int(thread["thread_pk"])
        latest = conn.execute(
            """
            SELECT subject, is_from_me FROM messages
            WHERE thread_pk = ?
            ORDER BY internal_date DESC, msg_pk DESC
            LIMIT 1
            """,
            (thread_pk,),
        ).fetchone()
        conn.execute(
            """
            UPDATE threads SET
              msg_count = (SELECT count(*) FROM messages WHERE thread_pk = ?),
              first_msg_at = (SELECT min(internal_date) FROM messages WHERE thread_pk = ?),
              last_msg_at = (SELECT max(internal_date) FROM messages WHERE thread_pk = ?),
              last_msg_from_me = ?,
              subject_norm = ?
            WHERE thread_pk = ?
            """,
            (
                thread_pk,
                thread_pk,
                thread_pk,
                None if latest is None else latest["is_from_me"],
                None if latest is None else subject_norm(latest["subject"]),
                thread_pk,
            ),
        )


def _recompute_senders(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute(
        """
        UPDATE senders SET
          msg_count = (
            SELECT count(*) FROM messages WHERE messages.sender_id = senders.sender_id
          ),
          first_seen_at = (
            SELECT min(internal_date) FROM messages WHERE messages.sender_id = senders.sender_id
          ),
          last_seen_at = (
            SELECT max(internal_date) FROM messages WHERE messages.sender_id = senders.sender_id
          ),
          has_list_unsubscribe = (
            SELECT CASE WHEN EXISTS (
              SELECT 1 FROM messages
              JOIN headers ON headers.msg_pk = messages.msg_pk
              WHERE messages.sender_id = senders.sender_id
                AND headers.name = 'list-unsubscribe'
            ) THEN 1 ELSE 0 END
          )
        WHERE account_id = ?
        """,
        (account_id,),
    )


def _touch_label_seen(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute(
        """
        UPDATE labels SET seen_at = COALESCE((
          SELECT max(messages.internal_date)
          FROM message_labels
          JOIN messages ON messages.msg_pk = message_labels.msg_pk
          WHERE message_labels.account_id = labels.account_id
            AND message_labels.gmail_label_id = labels.gmail_label_id
        ), seen_at)
        WHERE account_id = ?
        """,
        (account_id,),
    )


def _append_ledger(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    ref: str,
    detail: dict[str, object],
) -> None:
    prev = conn.execute(
        "SELECT row_hash FROM ledger ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    prev_hash = "" if prev is None else str(prev["row_hash"])
    ts = int(time.time())
    detail_json = json.dumps(detail, sort_keys=True, separators=(",", ":"))
    canonical = json.dumps(
        {
            "account_id": account_id,
            "action": "import",
            "actor": "mailcrawl",
            "detail": detail,
            "prev_hash": prev_hash,
            "ref": ref,
            "ts": ts,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    row_hash = hashlib.sha256((prev_hash + canonical).encode()).hexdigest()
    conn.execute(
        """
        INSERT INTO ledger(ts, actor, action, account_id, ref, detail_json, prev_hash, row_hash)
        VALUES (?, 'mailcrawl', 'import', ?, ?, ?, ?, ?)
        """,
        (ts, account_id, ref, detail_json, prev_hash, row_hash),
    )
