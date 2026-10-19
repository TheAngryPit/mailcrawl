# SPDX-License-Identifier: MIT
"""Idempotent import of a gog export or a Takeout mbox through one writer."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from itertools import chain
from mailcrawl import inputio

from mailcrawl.clock import as_of
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
from mailcrawl.taxonomy import axis_of, generic_category, load_taxonomy
from mailcrawl.store import index_message
from mailcrawl.owner import classify_from_me
from mailcrawl.takeout_labels import checked_label_map
from mailcrawl.takeout_repair import repair_takeout
from mailcrawl.textnorm import clean_body, parse_address, subject_norm
from mailcrawl.threads import embeddable_image, mark_image_only
from mailcrawl.version import mailcrawl_version, provenance_hash

Item = LabelRec | MessageRec | Refused
_DIAGNOSTIC_SAMPLE_LIMIT = 100


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
        takeout_unmapped: tuple[str, ...] = (),
    ) -> None:
        self.n_seen = n_seen
        self.n_new = n_new
        self.n_dup = n_dup
        self.n_error = n_error
        self.n_refused = n_refused
        self.n_reconciled = n_reconciled
        self.takeout_unmapped = takeout_unmapped


def load_catalogue_entries(path: Path | None, alias: str) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    return load_taxonomy(path).catalogue(alias)


def import_gog_export(
    conn: sqlite3.Connection,
    *,
    alias: str,
    export_dir: Path,
    taxonomy_path: Path | None,
    account_email: str,
    aliases: frozenset[str] = frozenset(),
    limits: inputio.ImportLimits | None = None,
    on_success: Callable[[ImportResult], None] | None = None,
) -> ImportResult:
    policy = inputio.DEFAULT_GOG_IMPORT_LIMITS if limits is None else limits
    parsed, manifest_sha = _load_export(export_dir, limits=policy)
    account_id = _account_id(conn, alias)
    return _ingest(
        conn,
        account_id=account_id,
        alias=alias,
        source="gog-export",
        items=chain(parsed.labels, parsed.messages),
        ref=lambda: manifest_sha,
        run_json=parsed.raw_json,
        catalogue=load_catalogue_entries(taxonomy_path, alias),
        account_email=account_email,
        aliases=aliases,
        limits=policy,
        on_success=on_success,
    )


def import_mbox(
    conn: sqlite3.Connection,
    *,
    alias: str,
    mbox_path: Path,
    labels_path: Path | None,
    taxonomy_path: Path | None,
    account_email: str,
    label_map: dict[str, str] | None = None,
    aliases: frozenset[str] = frozenset(),
    limits: inputio.ImportLimits | None = None,
    on_success: Callable[[ImportResult], None] | None = None,
) -> ImportResult:
    policy = inputio.DEFAULT_MBOX_IMPORT_LIMITS if limits is None else limits
    account_id = _account_id(conn, alias)
    mapping = checked_label_map(label_map)
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
        label_map=mapping,
        limits=policy,
        stored=lambda key: conn.execute(
            "SELECT 1 FROM messages WHERE account_id = ? AND gmail_message_id = ?",
            (account_id, key),
        ).fetchone()
        is not None,
    )
    def finish(result: ImportResult) -> None:
        report = repair_takeout(conn, label_map, aliases=aliases)
        result.takeout_unmapped = report.unmapped
        if on_success is not None:
            on_success(result)

    return _ingest(
        conn,
        account_id=account_id,
        alias=alias,
        source="mbox",
        items=source.items(),
        ref=source.sha256,
        run_json=None,
        catalogue=load_catalogue_entries(taxonomy_path, alias),
        account_email=account_email,
        aliases=aliases,
        limits=policy,
        on_success=finish,
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
    aliases: frozenset[str] = frozenset(),
    limits: inputio.ImportLimits | None = None,
    on_success: Callable[[ImportResult], None] | None = None,
) -> ImportResult:
    version = mailcrawl_version()
    me = account_email.strip().lower()
    started = as_of(None)
    counts = {"new": 0, "dup": 0, "conflict": 0, "reconciled": 0}
    conflicts: list[str] = []
    reconciled: list[dict[str, str]] = []
    refused: list[dict[str, object]] = []
    refused_count = 0
    input_records = 0
    known_labels = {row[0] for row in conn.execute("SELECT gmail_label_id FROM labels WHERE account_id = ?", (account_id,))}
    policy = inputio.DEFAULT_GOG_IMPORT_LIMITS if limits is None else limits
    budget = inputio.ImportBudget(limits=policy)
    budget_token = inputio.import_budget.set(budget)
    try:
        for item in items:
            if isinstance(item, LabelRec):
                if source == "mbox" or not is_pseudo_label(item.gmail_label_id):
                    _upsert_label(conn, account_id, item, catalogue)
                    known_labels.add(item.gmail_label_id)
                continue
            if isinstance(item, Refused):
                input_records += 1
                if policy.max_records is not None and input_records > policy.max_records:
                    raise inputio.ImportExhausted("import record limit exceeded")
                refused_count += 1
                if len(refused) < _DIAGNOSTIC_SAMPLE_LIMIT:
                    refused.append({"offset": item.offset, "reason": item.reason})
                continue
            input_records += 1
            if policy.max_records is not None and input_records > policy.max_records:
                raise inputio.ImportExhausted("import record limit exceeded")
            for label_id in item.label_ids:
                if label_id not in known_labels and (source == "mbox" or not is_pseudo_label(label_id)):
                    _upsert_label(conn, account_id, LabelRec(label_id, label_id, infer_kind(label_id)), catalogue)
                    known_labels.add(label_id)
            if any(len(a.image_bytes or b"") > inputio.MAX_IMAGE_BYTES for a in item.attachments):
                refused_count += 1
                if len(refused) < _DIAGNOSTIC_SAMPLE_LIMIT:
                    refused.append({"offset": 0, "reason": "image_limit"})
                continue
            outcome, kept_sha = _upsert_message(
                conn,
                account_id=account_id,
                alias=alias,
                source=source,
                message=item,
                version=version,
                me=me,
                aliases=aliases,
            )
            counts[outcome] += 1
            if outcome == "conflict":
                if len(conflicts) < _DIAGNOSTIC_SAMPLE_LIMIT:
                    conflicts.append(item.gmail_message_id)
            elif outcome == "reconciled":
                if len(reconciled) < _DIAGNOSTIC_SAMPLE_LIMIT:
                    reconciled.append(
                        {
                            "gmail_message_id": item.gmail_message_id,
                            "kept_raw_sha256": kept_sha,
                            "seen_raw_sha256": item.raw_sha256,
                        }
                    )
        manifest_sha = ref()
        n_seen = sum(counts.values()) + refused_count
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
        finished = as_of(None)
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
                "n_refused": refused_count,
                "n_seen": n_seen,
                "reconciled": reconciled,
                "refused": refused,
                "source": source,
            },
        )
        result = ImportResult(
            n_seen=n_seen,
            n_new=counts["new"],
            n_dup=counts["dup"],
            n_error=counts["conflict"],
            n_refused=refused_count,
            n_reconciled=counts["reconciled"],
        )
        if on_success is not None:
            on_success(result)
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        finally:
            budget.remove_created_images()
        raise
    finally:
        inputio.import_budget.reset(budget_token)
        budget.close()
    return result


def _load_export(
    export_dir: Path,
    *,
    limits: inputio.ImportLimits | None = None,
) -> tuple[ExportDocument, str]:
    export_path = export_dir / "export.json"
    try:
        reader = inputio.SafeRoot(export_dir)
    except OSError as exc:
        from mailcrawl.gognative import ExportInvalid
        raise ExportInvalid("unsupported_schema", "export directory is missing", 3) from exc
    try:
        raw_file = reader.read("export.json", inputio.MAX_MANIFEST_BYTES)
    except FileNotFoundError:
        reader.close()
        return load_native(export_dir, limits=limits)
    except inputio.UnsafeInput as exc:
        reader.close()
        from mailcrawl.gognative import ExportInvalid
        raise ExportInvalid("unsafe_path", "unsafe export manifest path", 1) from exc
    except BaseException:
        reader.close()
        raise
    try:
        document = json.loads(raw_file.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        reader.close()
        raise ValueError("invalid export manifest JSON") from exc
    try:
        parsed = parse_export(document, reader, limits=limits)
    except BaseException:
        reader.close()
        raise
    return parsed, hashlib.sha256(raw_file).hexdigest()


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
    if entry is None and not pseudo:
        generic = generic_category(label.gmail_label_id)
        if generic is not None:
            entry = {"key": generic, "axis": axis_of(generic)}
    in_catalogue = 1 if entry is not None else 0
    catalogue_key = None
    axis = None
    if entry is not None:
        catalogue_key = entry["key"]
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
    aliases: frozenset[str] = frozenset(),
) -> tuple[str, str | None]:
    """Return the outcome and, when a row already existed, its kept ``raw_sha256``.

    A later gog import keeps an existing mbox row even when the hash differs,
    refreshes its labels, and moves it off a provisional thread. An mbox import
    never touches a row that came from another source.
    """
    thread_pk = _ensure_thread(conn, account_id, message.gmail_thread_id)
    sender_id, _address = _ensure_sender(conn, account_id, message.from_hdr)
    verdict = classify_from_me(
        has_sent="SENT" in message.label_ids,
        from_header=message.from_hdr,
        auth_results=message.auth_results,
        aliases=aliases,
    )
    is_from_me = int(verdict.mine)
    from_me_origin = verdict.origin
    prov = provenance_hash(alias, message.gmail_message_id, message.raw_sha256, version)
    raw_body = message.body_text
    cleaned = clean_body(raw_body)
    inserted = conn.execute(
        """
        INSERT INTO messages(
          account_id, gmail_message_id, thread_pk, gmail_history_id, internal_date,
          rfc822_message_id, in_reply_to, references_hdr, sender_id,
          from_hdr, to_hdr, cc_hdr, bcc_hdr, reply_to_hdr,
          subject, snippet, body_text_raw, body_text, size_bytes, is_from_me, from_me_origin, date_trusted,
          raw_path, raw_sha256, raw_json, auth_results, provenance_hash, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            from_me_origin,
            message.date_trusted,
            message.raw_path,
            message.raw_sha256,
            message.raw_json,
            message.auth_results,
            prov,
            as_of(None),
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
    if source == "mbox":
        conn.execute(
            """
            UPDATE messages
            SET is_from_me = ?, from_me_origin = ?, internal_date = ?, date_trusted = ?, raw_path = ?
            WHERE msg_pk = ?
            """,
            (is_from_me, from_me_origin, message.internal_date, message.date_trusted, message.raw_path, msg_pk),
        )
    else:
        conn.execute(
            """
            UPDATE messages
            SET is_from_me = ?, from_me_origin = ?, internal_date = ?, date_trusted = ?
            WHERE msg_pk = ?
            """,
            (is_from_me, from_me_origin, message.internal_date, message.date_trusted, msg_pk),
        )
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
    index_message(
        conn,
        msg_pk,
        message.subject,
        clean_body(message.body_text),
        message.from_hdr,
        attachment_text,
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


def _keep_image(conn: sqlite3.Connection, attachment: AttachmentRec) -> str | None:
    blob = attachment.image_bytes
    if not blob or not embeddable_image(attachment.mime_type, attachment.is_inline, attachment.size_bytes):
        return None
    listed = conn.execute("PRAGMA database_list").fetchone()
    db_file = "" if listed is None else str(listed[2] or "")
    if not db_file:
        return None
    if len(blob) > inputio.MAX_IMAGE_BYTES:
        raise inputio.InputLimit("image byte limit exceeded")
    budget = inputio.import_budget.get()
    directory = Path(db_file).parent / "images"
    directory.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.sha256(blob).hexdigest()
    path = directory / digest
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(digest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            with inputio.SafeRoot(directory) as reader:
                reader.read(digest, inputio.MAX_IMAGE_BYTES)
        else:
            try:
                if (
                    budget is not None
                    and budget.limits.max_image_bytes is not None
                    and budget.image_bytes + len(blob) > budget.limits.max_image_bytes
                ):
                    raise inputio.ImportExhausted("import image byte limit exceeded")
                if budget is not None:
                    budget.track_image(path)
                    budget.image_bytes += len(blob)
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(blob)
            except BaseException:
                if fd >= 0:
                    os.close(fd)
                    os.unlink(digest, dir_fd=directory_fd)
                raise
    finally:
        os.close(directory_fd)
    return str(path)


def _insert_attachment(
    conn: sqlite3.Connection,
    msg_pk: int,
    attachment: AttachmentRec,
) -> None:
    stored_path = _keep_image(conn, attachment)
    conn.execute(
        """
        INSERT INTO attachments(
          msg_pk, filename, mime_type, size_bytes, gmail_attachment_id,
          content_id, is_inline, file_sha256, stored_path, extract_status, extract_method,
          extracted_text, ocr_status, ocr_conf, page_count, parser_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            stored_path,
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
            SELECT subject FROM messages
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
              subject_norm = ?
            WHERE thread_pk = ?
            """,
            (
                thread_pk,
                thread_pk,
                thread_pk,
                None if latest is None else subject_norm(latest["subject"]),
                thread_pk,
            ),
        )
    mark_image_only(conn, account_id)


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
    ts = as_of(None)
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
